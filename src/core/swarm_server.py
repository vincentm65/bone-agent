"""Swarm server — WebSocket message bus for the swarm pool.

Runs as a background thread in the admin process. Manages:
- Worker connections (auto-assigns IDs)
- Task queueing and dispatch
- Approval routing with synchronous policy evaluation
- Admin commands (approve/deny)
- Status snapshots for toolbar display
- Inbox queue for admin LLM processing

Two-thread architecture: the WebSocket asyncio thread (Thread A) handles
I/O and synchronous policy decisions. The main prompt_toolkit thread
(Thread B) drains the inbox and calls the LLM. No event bus, no
controller thread, no synthetic prompts.
"""

import asyncio
import json
import logging
import queue
import secrets
import threading
import time
import uuid
from typing import Any, Optional

from core.swarm_approval import ApprovalDecision, evaluate_swarm_approval

logger = logging.getLogger(__name__)


class SwarmServer:
    """WebSocket server for swarm pool management.

    Runs on a configurable host:port. Workers connect via ws://host:port/<swarm_name>.
    The admin terminal communicates through the server's inbox queue.
    """

    def __init__(self, swarm_name: str, host: str = "127.0.0.1", port: int = 8765):
        self.swarm_name = swarm_name
        self.host = host
        self.port = port
        self.base_url = f"ws://{host}:{port}/{swarm_name}"
        self._auth_token = secrets.token_hex(16)

        # Worker tracking: worker_id -> {websocket, status, current_task_id}
        self._workers: dict[str, dict[str, Any]] = {}
        self._worker_counter = 0
        self._next_worker_index = 0

        # Task tracking: task_id -> {prompt, write_scope, status, worker_id, summary}
        self._tasks: dict[str, dict[str, Any]] = {}
        self._task_queue: list[dict[str, Any]] = []  # FIFO queue of pending tasks

        # Approval tracking: (task_id, call_id) -> pending_approval
        self._pending_approvals: dict[tuple[str, str], dict[str, Any]] = {}

        # Notification history (for /swarm results)
        self._max_notifications: int = 100
        self._notification_history: list[dict[str, Any]] = []

        # Inbox queue: messages for the admin LLM (completions, human-required approvals)
        self._inbox: queue.Queue[dict[str, Any]] = queue.Queue()

        # prompt_toolkit Application reference for toolbar invalidation
        self._app: Any = None

        # Server state
        self._server = None
        self._running = False
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._dispatch_start_timeout_seconds = 15
        self._last_error: str | None = None
        self._error_lock = threading.Lock()

    @property
    def workers(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self._workers)

    @property
    def tasks(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self._tasks)

    @property
    def pending_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._task_queue)

    @property
    def worker_count(self) -> int:
        with self._lock:
            return len(self._workers)

    @property
    def idle_workers(self) -> list[str]:
        with self._lock:
            return [wid for wid, w in self._workers.items() if w["status"] == "idle"]

    @property
    def connection_info(self) -> dict[str, str]:
        """Connection details including the auth token.

        The admin can relay these to workers so they can authenticate.
        """
        return {
            "url": self.base_url,
            "auth_token": self._auth_token,
        }

    def _next_idle_worker_id(self) -> str | None:
        """Pick the next idle worker using a round-robin cursor."""
        with self._lock:
            worker_ids = list(self._workers.keys())
            if not worker_ids:
                self._next_worker_index = 0
                return None

            self._next_worker_index %= len(worker_ids)
            for offset in range(len(worker_ids)):
                index = (self._next_worker_index + offset) % len(worker_ids)
                worker_id = worker_ids[index]
                if self._workers[worker_id]["status"] == "idle":
                    self._next_worker_index = (index + 1) % len(worker_ids)
                    return worker_id

            return None

    def start(self) -> bool:
        """Start the server in a background thread.

        On bind-in-use errors, automatically increments the port up to
        20 times.  ``self.port`` and ``self.base_url`` reflect the
        actual bound port on success.

        Returns:
            True if server started successfully. On failure, check
            ``self._last_error`` for the reason.
        """
        MAX_RETRIES = 20

        for attempt in range(MAX_RETRIES + 1):
            try:
                if self._thread and self._thread.is_alive():
                    if self._loop and not self._loop.is_closed():
                        self._loop.call_soon_threadsafe(self._loop.stop)
                    self._thread.join(timeout=3)
                    if self._thread.is_alive():
                        logger.warning(
                            "Old server thread still alive after stop signal on port %d",
                            self.port,
                        )
                if self._loop and not self._loop.is_closed():
                    self._loop.close()
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=lambda: self._loop.run_until_complete(self._serve()),
                    daemon=True,
                )
                self._thread.start()
                # Give the server a moment to bind
                for _ in range(50):  # 5 seconds max wait
                    if self._running:
                        return True
                    with self._error_lock:
                        if self._last_error:
                            break
                    time.sleep(0.1)

                # Server didn't start. Check if _serve already set an error.
                with self._error_lock:
                    has_error = self._last_error is not None
                if not has_error:
                    # Timeout with no error set
                    with self._error_lock:
                        self._last_error = (
                            f"Server failed to start on {self.host}:{self.port} "
                            "(timed out waiting for ready signal). "
                            "The port may already be in use. Run ``/swarm close`` first "
                            "or use a different port in your config."
                        )
                    return False

                # Check if it's an address-in-use error worth retrying.
                with self._error_lock:
                    error_lower = self._last_error.lower()
                if any(kw in error_lower for kw in
                       ("address already in use", "eaddrinuse", "errno 98", "10048")):
                    if attempt >= MAX_RETRIES:
                        with self._error_lock:
                            self._last_error = (
                                f"Failed to start server after {MAX_RETRIES} port retries "
                                f"(last tried {self.host}:{self.port}). "
                                f"All ports up to {self.host}:{self.port} are occupied."
                            )
                        return False

                    old_port = self.port
                    self.port += 1
                    self.base_url = f"ws://{self.host}:{self.port}/{self.swarm_name}"
                    with self._error_lock:
                        self._last_error = None
                    logger.info("Port %d in use, trying %d...", old_port, self.port)
                    continue
                else:
                    # Unrelated error — fail immediately, don't retry.
                    return False

            except Exception as e:
                with self._error_lock:
                    self._last_error = f"Failed to start swarm server: {e}"
                logger.error("Failed to start swarm server: %s", e)
                return False

    async def _serve(self):
        """Main async server loop."""
        import websockets

        try:
            self._server = await websockets.serve(
                self._handle_connection,
                self.host,
                self.port,
                ping_interval=30,
                ping_timeout=30,
            )
            self._running = True
            logger.info("Swarm server running on ws://%s:%d", self.host, self.port)
        except OSError as e:
            # Bind-in-use errors are expected during port auto-increment.
            # start() will retry — don't spam the terminal.
            self._last_error = f"Failed to bind to {self.host}:{self.port}: {e}"
            error_lower = str(e).lower()
            if any(kw in error_lower for kw in ("address already in use", "eaddrinuse", "errno 98", "10048")):
                logger.debug("Port %d in use: %s", self.port, e)
            else:
                logger.error("Server failed to start: %s", e)
            return
        except Exception as e:
            self._last_error = f"Failed to bind to {self.host}:{self.port}: {e}"
            logger.error("Server failed to start: %s", e)
            return

        try:
            await self._server.wait_closed()
        finally:
            self._running = False

    async def _handle_connection(self, websocket):
        """Handle a new WebSocket connection (worker or admin)."""
        try:
            # Wait for worker_hello
            raw = await asyncio.wait_for(websocket.recv(), timeout=30)
            msg = json.loads(raw)

            if msg.get("type") != "worker_hello":
                await websocket.close(1008, "Expected worker_hello")
                return

            # Validate auth token (skip for localhost connections)
            remote_addr = websocket.remote_address
            is_local = (
                remote_addr
                and remote_addr[0] in ("127.0.0.1", "::1")
            )
            if not is_local and msg.get("token") != self._auth_token:
                await websocket.close(1008, "Invalid auth token")
                return

            # Extract identity payload
            display_name = msg.get("display_name", "")
            model = msg.get("model", "")
            provider = msg.get("provider", "")

            # Assign worker ID and register (under lock)
            with self._lock:
                self._worker_counter += 1
                worker_id = f"worker-{self._worker_counter:02d}"

                # Register worker
                self._workers[worker_id] = {
                    "websocket": websocket,
                    "status": "idle",
                    "current_task_id": None,
                    "display_name": display_name or "",
                    "model": model or "",
                    "provider": provider or "",
                    "current_activity": "",
                }

                # Store event for notification history
                self._store_event(
                    f"{worker_id} joined the swarm",
                    extra={"kind": "worker_joined", "worker_id": worker_id, "status": "connected"},
                )

            # Send assigned ID before any queued task dispatch.
            await websocket.send(json.dumps({
                "type": "worker_join",
                "worker_id": worker_id,
            }))

            # A task may have been queued before any worker was connected.
            await self._dispatch_queued_tasks_async()

            # Start message handling for this worker
            await self._handle_worker_messages(worker_id, websocket)

        except Exception as e:
            # Suppress expected disconnect errors — workers disconnecting is normal.
            import websockets.exceptions
            if isinstance(e, (websockets.exceptions.ConnectionClosed, ConnectionResetError, ConnectionAbortedError, ConnectionError)):
                logger.debug("Worker connection closed: %s", e)
            else:
                logger.warning("Connection handler error: %s", e)

    async def _handle_worker_messages(self, worker_id: str, websocket):
        """Handle messages from a specific worker."""
        try:
            while self._running:
                self._check_dispatch_watchdog()
                try:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=1)
                except asyncio.TimeoutError:
                    continue

                msg = json.loads(raw)
                msg_type = msg.get("type")

                if msg_type == "task_started":
                    task_id = msg.get("task_id", "")
                    with self._lock:
                        if task_id not in self._tasks:
                            self._store_event(
                                f"Ignoring task_started for unknown task {task_id} from {worker_id}",
                                extra={"kind": "warning", "task_id": task_id, "worker_id": worker_id, "status": "unknown_task"},
                            )
                            skip_dispatch = True
                        else:
                            self._tasks[task_id]["status"] = "running"
                            self._tasks[task_id]["worker_id"] = worker_id
                            self._tasks[task_id]["started_at"] = time.time()
                            self._workers[worker_id]["status"] = "running"
                            self._workers[worker_id]["current_task_id"] = task_id
                            self._store_event(
                                f"Task {task_id} started on {worker_id}",
                                extra={"kind": "task_started", "task_id": task_id, "worker_id": worker_id, "status": "started"},
                            )
                            skip_dispatch = False
                    if skip_dispatch:
                        continue
                    # Try to dispatch next queued task
                    await self._dispatch_queued_tasks_async()

                elif msg_type == "completion_summary":
                    task_id = msg.get("task_id", "")
                    summary = msg.get("message", "")
                    status = msg.get("status", "done")
                    user_intervened = msg.get("user_intervened", False)

                    with self._lock:
                        if task_id not in self._tasks:
                            self._store_event(
                                f"Ignoring completion for unknown task {task_id} from {worker_id}",
                                extra={"task_id": task_id, "worker_id": worker_id, "status": "unknown_task"},
                            )
                            skip_dispatch = True
                        else:
                            self._tasks[task_id]["status"] = status
                            self._tasks[task_id]["summary"] = summary

                            self._workers[worker_id]["status"] = "idle"
                            self._workers[worker_id]["current_task_id"] = None
                            self._workers[worker_id]["current_activity"] = ""

                            intervention_note = " (user intervened)" if user_intervened else ""

                            # Store event for notification history
                            self._store_event(
                                f"Task {task_id} {status} on {worker_id}{intervention_note}",
                                extra={
                                    "kind": "task_completed",
                                    "task_id": task_id,
                                    "worker_id": worker_id,
                                    "status": status,
                                    "summary": summary,
                                },
                            )

                            # Post completion to inbox for admin LLM processing
                            self._inbox.put({
                                "kind": "completion",
                                "task_id": task_id,
                                "worker_id": worker_id,
                                "display_name": self._workers[worker_id].get("display_name", ""),
                                "status": status,
                                "summary": summary,
                            })
                            skip_dispatch = False
                    if skip_dispatch:
                        continue

                    # Dispatch next queued task if available
                    await self._dispatch_queued_tasks_async()

                elif msg_type == "approval_request":
                    task_id = msg.get("task_id", "")
                    call_id = msg.get("call_id", "")
                    command = msg.get("command", "")
                    worker_id_msg = msg.get("worker_id", worker_id)

                    # Evaluate approval policy synchronously (pure function, no lock needed).
                    result = evaluate_swarm_approval(command)

                    if result.decision == ApprovalDecision.APPROVED:
                        # Auto-approve — respond immediately, never involve the LLM.
                        response = {
                            "type": "approval_response",
                            "task_id": task_id,
                            "call_id": call_id,
                            "approved": True,
                            "guidance": result.reason,
                        }
                        await websocket.send(json.dumps(response))
                        with self._lock:
                            self._store_event(
                                f"Auto-approved {task_id}/{call_id}: {command[:80]}",
                                extra={
                                    "kind": "approval_resolved",
                                    "task_id": task_id,
                                    "call_id": call_id,
                                    "worker_id": worker_id_msg,
                                    "status": "approved",
                                    "command": command,
                                },
                            )
                        continue  # Continue the message loop

                    elif result.decision == ApprovalDecision.DENIED:
                        # Auto-deny — respond immediately.
                        response = {
                            "type": "approval_response",
                            "task_id": task_id,
                            "call_id": call_id,
                            "approved": False,
                            "guidance": result.reason,
                        }
                        await websocket.send(json.dumps(response))
                        with self._lock:
                            self._store_event(
                                f"Auto-denied {task_id}/{call_id}: {command[:80]}",
                                extra={
                                    "kind": "approval_resolved",
                                    "task_id": task_id,
                                    "call_id": call_id,
                                    "worker_id": worker_id_msg,
                                    "status": "denied",
                                    "command": command,
                                },
                            )
                        continue  # Continue the message loop

                    # REQUIRES_HUMAN — queue for admin LLM.
                    with self._lock:
                        key = (task_id, call_id)
                        self._pending_approvals[key] = {
                            "worker_id": worker_id_msg,
                            "task_id": task_id,
                            "call_id": call_id,
                            "command": command,
                            "preview": msg.get("preview", ""),
                            "reason": msg.get("reason", ""),
                            "websocket": websocket,
                        }

                        # Mark worker as blocked
                        self._workers[worker_id_msg]["status"] = "blocked"

                        command_preview = command[:160].rstrip()
                        if len(command) > 160:
                            command_preview += "..."

                        # Store event for notification history
                        self._store_event(
                            f"{worker_id_msg} requests approval: {command_preview}",
                            extra={
                                "kind": "approval_requested",
                                "task_id": task_id,
                                "call_id": call_id,
                                "worker_id": worker_id_msg,
                                "status": "approval_pending",
                                "command": command,
                                "command_preview": command_preview,
                            },
                        )

                        # Put in inbox for admin LLM to handle
                        self._inbox.put({
                            "kind": "approval_needed",
                            "task_id": task_id,
                            "call_id": call_id,
                            "worker_id": worker_id_msg,
                            "display_name": self._workers[worker_id_msg].get("display_name", ""),
                            "command": command,
                            "command_preview": command_preview,
                            "reason": msg.get("reason", ""),
                        })

                elif msg_type == "approval_cancelled":
                    task_id = msg.get("task_id", "")
                    call_id = msg.get("call_id", "")
                    reason = msg.get("reason", "Approval cancelled")
                    worker_id_msg = msg.get("worker_id", worker_id)
                    self._clear_pending_approval(task_id, call_id, worker_id_msg, reason)

                elif msg_type == "stop_worker":
                    # Admin-initiated stop — already handled by _stop_worker
                    return

                elif msg_type == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))

        except Exception as e:
            # Suppress expected disconnect errors — workers disconnecting is normal.
            import websockets.exceptions
            if isinstance(e, (websockets.exceptions.ConnectionClosed, ConnectionResetError, ConnectionAbortedError, ConnectionError)):
                logger.debug("Worker %s disconnected: %s", worker_id, e)
            else:
                logger.warning("Worker %s message loop error: %s", worker_id, e)
        finally:
            # Worker disconnected
            self._store_event(
                f"{worker_id} disconnected",
                extra={"kind": "worker_left", "worker_id": worker_id},
            )
            self._cleanup_worker(worker_id)

    def _cleanup_worker(self, worker_id: str) -> None:
        """Remove a worker and mark their task as interrupted."""
        with self._lock:
            worker = self._workers.pop(worker_id, None)
            if self._workers:
                self._next_worker_index %= len(self._workers)
            else:
                self._next_worker_index = 0
            if worker:
                task_id = worker.get("current_task_id")
                if task_id and task_id in self._tasks:
                    self._tasks[task_id]["status"] = "interrupted"
                    self._store_event(
                        f"Task {task_id} interrupted — {worker_id} disconnected",
                        extra={"kind": "warning", "task_id": task_id, "worker_id": worker_id, "status": "interrupted"},
                    )
            for key, pending in list(self._pending_approvals.items()):
                if pending.get("worker_id") == worker_id:
                    self._pending_approvals.pop(key, None)

    def _clear_pending_approval(self, task_id: str, call_id: str,
                                worker_id: str, reason: str) -> None:
        """Remove a pending approval after the worker stops waiting for it."""
        with self._lock:
            key = (task_id, call_id)
            pending = self._pending_approvals.pop(key, None)
            if pending is None:
                return

            if worker_id in self._workers and self._workers[worker_id]["status"] == "blocked":
                self._workers[worker_id]["status"] = "running"

            self._store_event(
                f"Approval cancelled for {task_id}/{call_id}: {reason}",
                extra={"kind": "approval_cancelled", "task_id": task_id, "call_id": call_id, "status": "approval_cancelled"},
            )

    async def _dispatch_queued_tasks_async(self) -> None:
        """Dispatch queued tasks to idle workers from the server event loop."""
        while self._task_queue and self.idle_workers:
            # Capture task + worker under lock, then release for the send.
            with self._lock:
                if not self._task_queue:
                    break
                task = self._task_queue.pop(0)
                worker_id = self._next_idle_worker_id()
                if not worker_id:
                    self._task_queue.insert(0, task)
                    break

                self._tasks[task["task_id"]] = {
                    "prompt": task["prompt"],
                    "write_scope": task.get("write_scope", []),
                    "status": "dispatched",
                    "worker_id": worker_id,
                    "summary": "",
                    "dispatched_at": time.time(),
                    "dispatch_watchdog_notified": False,
                    "plan_index": task.get("plan_index"),
                }

                self._workers[worker_id]["status"] = "dispatched"
                self._workers[worker_id]["current_task_id"] = task["task_id"]
                self._workers[worker_id]["current_activity"] = task.get("activity_label", "")

            # Send outside the lock to avoid blocking on websocket I/O.
            sent = await self._send_to_worker_async(
                worker_id,
                {
                    "type": "task_dispatch",
                    "task_id": task["task_id"],
                    "prompt": task["prompt"],
                    "write_scope": task.get("write_scope", []),
                    "activity_label": task.get("activity_label", ""),
                },
            )

            # Reacquire for post-send state updates.
            with self._lock:
                if sent:
                    self._store_event(
                        f"Dispatched task {task['task_id']} to {worker_id}",
                        extra={"kind": "task_dispatched", "task_id": task["task_id"], "worker_id": worker_id},
                    )
                else:
                    self._workers[worker_id]["status"] = "idle"
                    self._workers[worker_id]["current_task_id"] = None
                    self._tasks.pop(task["task_id"], None)
                    self._task_queue.insert(0, task)
                    break

    def _dispatch_queued_tasks(self) -> None:
        """Dispatch queued tasks to idle workers from non-event-loop callers."""
        if not self._loop or not self._loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(
            self._dispatch_queued_tasks_async(),
            self._loop,
        ).result(timeout=10)

    def _check_dispatch_watchdog(self) -> None:
        """Notify admin when a dispatched task never reports task_started."""
        with self._lock:
            now = time.time()
            for task_id, task in self._tasks.items():
                if task.get("status") != "dispatched":
                    continue
                if task.get("dispatch_watchdog_notified"):
                    continue
                dispatched_at = task.get("dispatched_at")
                if not dispatched_at:
                    continue
                if now - dispatched_at < self._dispatch_start_timeout_seconds:
                    continue
                task["dispatch_watchdog_notified"] = True
                worker_id = task.get("worker_id")
                self._store_event(
                    f"Task {task_id} was dispatched to {worker_id} but has not started yet",
                    extra={"kind": "warning", "task_id": task_id, "worker_id": worker_id, "status": "dispatch_start_timeout"},
                )

    def submit_task(self, prompt: str, write_scope: list[str] | None = None,
                    task_id: str | None = None,
                    plan_index: int | None = None,
                    activity_label: str | None = None) -> dict:
        """Submit a task to the server queue.

        If idle workers are available, the task is dispatched immediately.
        Otherwise it is queued until a worker becomes available.

        Args:
            prompt: The task prompt text.
            write_scope: Expected files the worker will edit.
            task_id: Optional task ID (auto-generated if not provided).

        Returns:
            Dict with task_id, status, and either worker_id or queue_position.
        """
        task_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        task = {
            "task_id": task_id,
            "prompt": prompt,
            "write_scope": write_scope or [],
            "plan_index": plan_index,
            "activity_label": activity_label or "",
        }
        with self._lock:
            self._task_queue.append(task)
        self._dispatch_queued_tasks()

        # After dispatch, check if task was picked up (under lock).
        with self._lock:
            if task_id in self._tasks:
                return {"task_id": task_id, "status": "dispatched", "worker_id": self._tasks[task_id].get("worker_id")}

            return {
                "task_id": task_id,
                "status": "queued",
                "queue_position": len(self._task_queue),
            }

    def approve(self, task_id: str, call_id: str, guidance: str = "") -> bool:
        """Approve a pending command approval.

        Args:
            task_id: The task ID.
            call_id: The command call ID.
            guidance: Optional guidance for the worker.

        Returns:
            True when a pending approval was found and approved.
        """
        with self._lock:
            key = (task_id, call_id)
            pending = self._pending_approvals.pop(key, None)
            if pending is None:
                self._store_event(
                    f"No pending approval for {task_id}/{call_id}",
                    extra={"kind": "warning", "task_id": task_id, "call_id": call_id},
                )
                return False

            worker_id = pending["worker_id"]
            ws = pending.get("websocket")

        # Send response to the worker outside the lock (blocks on event loop).
        response = {
            "type": "approval_response",
            "task_id": task_id,
            "call_id": call_id,
            "approved": True,
            "guidance": guidance,
        }
        if ws:
            try:
                asyncio.run_coroutine_threadsafe(
                    ws.send(json.dumps(response)),
                    self._loop,
                ).result(timeout=10)
            except Exception as e:
                logger.warning("Failed to send approval response to worker %s: %s", worker_id, e)

        # Worker resumes the same running task after receiving the response.
        # Status revert and event storage always execute, even if the send failed.
        with self._lock:
            if worker_id in self._workers:
                self._workers[worker_id]["status"] = "running"

            self._store_event(
                f"Approved {task_id}/{call_id} (worker {worker_id})",
                extra={"kind": "approval_resolved", "task_id": task_id, "call_id": call_id, "worker_id": worker_id, "status": "approved"},
            )
        return True

    def deny(self, task_id: str, call_id: str, reason: str = "") -> bool:
        """Deny a pending command approval.

        Args:
            task_id: The task ID.
            call_id: The command call ID.
            reason: Reason for denial.

        Returns:
            True when a pending approval was found and denied.
        """
        with self._lock:
            key = (task_id, call_id)
            pending = self._pending_approvals.pop(key, None)
            if pending is None:
                self._store_event(
                    f"No pending approval for {task_id}/{call_id}",
                    extra={"kind": "warning", "task_id": task_id, "call_id": call_id},
                )
                return False

            worker_id = pending["worker_id"]
            ws = pending.get("websocket")

        # Send response to the worker outside the lock (blocks on event loop).
        response = {
            "type": "approval_response",
            "task_id": task_id,
            "call_id": call_id,
            "approved": False,
            "guidance": reason,
        }
        if ws:
            try:
                asyncio.run_coroutine_threadsafe(
                    ws.send(json.dumps(response)),
                    self._loop,
                ).result(timeout=10)
            except Exception as e:
                logger.warning("Failed to send denial response to worker %s: %s", worker_id, e)

        # Status revert and event storage always execute, even if the send failed.
        with self._lock:
            if worker_id in self._workers:
                self._workers[worker_id]["status"] = "running"

            self._store_event(
                f"Denied {task_id}/{call_id} (worker {worker_id})",
                extra={"kind": "approval_resolved", "task_id": task_id, "call_id": call_id, "worker_id": worker_id, "status": "denied"},
            )
        return True

    def stop_worker(self, worker_id: str) -> None:
        """Send stop_worker to a specific worker.

        Args:
            worker_id: The worker to stop.
        """
        self._send_to_worker(worker_id, {"type": "stop_worker"})
        self._store_event(
            f"Stopped {worker_id}",
            extra={"kind": "warning", "worker_id": worker_id, "status": "stopped"},
        )

    def clear_worker_context(self, worker_id: str) -> bool:
        """Send clear_worker_context to a specific worker.

        Args:
            worker_id: The worker whose context to clear.

        Returns:
            True if the message was sent successfully.
        """
        with self._lock:
            worker = self._workers.get(worker_id)
            if not worker:
                return False
            if worker["status"] not in ("idle", "blocked"):
                return False
        return self._send_to_worker(worker_id, {"type": "clear_worker_context"})

    def kill_worker(self, worker_id: str) -> bool:
        """Permanently kill and remove a worker from the swarm pool.

        Immediately removes the worker from dispatch eligibility, cancels
        its pending approvals, marks its current task as ``"killed"``, and
        notifies the admin.  A ``stop_worker`` message is also sent in an
        attempt to terminate the process gracefully.

        Args:
            worker_id: The worker to kill.

        Returns:
            True if the worker was found and killed; False if unknown.
        """
        with self._lock:
            if worker_id not in self._workers:
                self._store_event(
                    f"Cannot kill {worker_id}: unknown worker",
                    extra={"kind": "warning", "worker_id": worker_id, "status": "unknown_worker"},
                )
                return False

            # Capture task info before removal so we can mark it killed.
            worker = self._workers[worker_id]
            task_id = worker.get("current_task_id")

        # 1. Send stop_worker message if possible (outside lock).
        self._send_to_worker(worker_id, {"type": "stop_worker"})

        # 2-7. All state mutations under lock.
        with self._lock:
            # Remove worker from dict immediately (no new tasks can be dispatched).
            self._workers.pop(worker_id, None)

            # Normalize round-robin cursor (same as _cleanup_worker).
            if self._workers:
                self._next_worker_index %= len(self._workers)
            else:
                self._next_worker_index = 0

            # Mark current task as killed (not interrupted).
            if task_id and task_id in self._tasks:
                self._tasks[task_id]["status"] = "killed"
                self._tasks[task_id]["summary"] = (
                    "Task killed by admin — worker was force-removed from swarm."
                )
                self._tasks[task_id]["completed_at"] = time.time()
                self._store_event(
                    f"Task {task_id} killed — {worker_id} force-removed",
                    extra={"kind": "warning", "task_id": task_id, "worker_id": worker_id, "status": "killed"},
                )

            # Cancel pending approvals for this worker.
            for key, pending in list(self._pending_approvals.items()):
                if pending.get("worker_id") == worker_id:
                    self._pending_approvals.pop(key, None)

            # Notify admin.
            self._store_event(
                f"{worker_id} killed by admin",
                extra={"kind": "warning", "worker_id": worker_id, "status": "killed"},
            )

        # 8. Dispatch queued tasks to remaining workers (outside lock).
        self._dispatch_queued_tasks()

        return True

    async def _stop_all_async(self) -> None:
        """Send stop_worker to all workers and close the server."""
        # Capture worker snapshot + websocket refs under lock.
        with self._lock:
            worker_snapshot = [
                (worker_id, self._workers[worker_id].get("websocket"))
                for worker_id in list(self._workers.keys())
            ]

        # Send stop messages outside the lock.
        for worker_id, websocket in worker_snapshot:
            if websocket:
                try:
                    await websocket.send(json.dumps({"type": "stop_worker"}))
                except Exception as e:
                    logger.warning("Failed to send stop_worker to %s: %s", worker_id, e)

        # Clean up state under lock.
        with self._lock:
            for worker_id, _websocket in worker_snapshot:
                self._store_event(
                    f"Stopped {worker_id}",
                    extra={"kind": "warning", "worker_id": worker_id, "status": "stopped"},
                )
                self._workers.pop(worker_id, None)
            if self._server:
                self._server.close()
            self._running = False

        # Wait for server close outside the lock (async I/O).
        if self._server:
            await self._server.wait_closed()

    def stop_all(self, force: bool = False) -> None:
        """Send stop_worker to all workers and stop the server.

        Workers are given time to finish their current task before
        cleanup removes them. All worker state is cleared after
        the shutdown signal is sent.

        Args:
            force: If True, always close the websocket server directly
                even if the event loop is not running (handles crash recovery).
        """
        if self._loop and self._loop.is_running() and not force:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._stop_all_async(),
                    self._loop,
                ).result(timeout=10)
                return
            except Exception as e:
                logger.warning("Async stop failed, falling back to force: %s", e)

        # Either the loop is not running, or async stop timed out.
        # Close the websocket server directly to release the port.
        try:
            if self._server:
                self._server.close()
        except Exception as e:
            logger.warning("Failed to close websocket server: %s", e)
        finally:
            with self._lock:
                for worker_id in list(self._workers.keys()):
                    self._workers.pop(worker_id, None)
                self._running = False

    def status_snapshot(self) -> dict:
        """Get a compact status snapshot of the swarm.

        Returns:
            Dict with workers, tasks, queue info.
        """
        with self._lock:
            workers_info = {}
            for wid, w in self._workers.items():
                workers_info[wid] = {
                    "status": w["status"],
                    "current_task_id": w["current_task_id"],
                    "display_name": w.get("display_name", ""),
                    "model": w.get("model", ""),
                    "provider": w.get("provider", ""),
                    "current_activity": w.get("current_activity", ""),
                }

            tasks_info = {}
            for tid, t in self._tasks.items():
                tasks_info[tid] = {
                    "status": t["status"],
                    "worker_id": t.get("worker_id"),
                    "summary": t.get("summary", "")[:200],  # Truncate for display
                    "plan_index": t.get("plan_index"),
                    "prompt": t.get("prompt", "")[:80],
                }

            pending_approvals = []
            for pending in self._pending_approvals.values():
                task = self._tasks.get(pending.get("task_id", ""), {})
                worker_id = pending.get("worker_id", "")
                worker = self._workers.get(worker_id, {})

                pending_approvals.append({
                    "worker_id": worker_id,
                    "task_id": pending.get("task_id", ""),
                    "call_id": pending.get("call_id", ""),
                    "command": pending.get("command", ""),
                    "reason": pending.get("reason", ""),
                    "preview": pending.get("preview", ""),
                    "task_prompt": task.get("prompt", ""),
                    "task_write_scope": task.get("write_scope", []),
                    "task_status": task.get("status", "unknown"),
                    "worker_status": worker.get("status", "unknown"),
                })

            return {
                "swarm_name": self.swarm_name,
                "auth_token": self._auth_token,
                "worker_count": len(self._workers),
                "idle_workers": len([w for w in self._workers.values() if w["status"] == "idle"]),
                "running_tasks": len([t for t in self._tasks.values() if t["status"] == "running"]),
                "workers": workers_info,
                "tasks": tasks_info,
                "pending_tasks": list(self._task_queue),
                "queued_tasks": len(self._task_queue),
                "pending_approvals": len(pending_approvals),
                "approval_requests": pending_approvals,
            }

    async def _send_to_worker_async(self, worker_id: str, message: dict) -> bool:
        """Send a message to a worker from the server event loop."""
        with self._lock:
            worker = self._workers.get(worker_id)
            if not worker:
                return False
            ws = worker["websocket"]
            if not ws:
                return False

        # Send outside the lock to avoid blocking on websocket I/O.
        try:
            await ws.send(json.dumps(message))
            return True
        except Exception as e:
            logger.warning("Failed to send to %s: %s", worker_id, e)
            return False

    def _send_to_worker(self, worker_id: str, message: dict) -> bool:
        """Send a message to a specific worker.

        Args:
            worker_id: The target worker.
            message: Message dict to send.

        Returns:
            True if sent successfully.
        """
        if not self._loop or not self._loop.is_running():
            return False
        try:
            return asyncio.run_coroutine_threadsafe(
                self._send_to_worker_async(worker_id, message),
                self._loop,
            ).result(timeout=10)
        except Exception as e:
            logger.warning("Failed to send to %s: %s", worker_id, e)
            return False

    def _store_event(self, text: str, extra: dict | None = None) -> None:
        """Store a notification in history and invalidate the toolbar.

        Called from any thread for every swarm event. Acquires the instance
        lock to protect ``_notification_history``; the toolbar invalidation
        runs outside the lock to avoid blocking the UI thread.

        Args:
            text: Human-readable notification text.
            extra: Optional dict with structured data.
        """
        with self._lock:
            self._notification_history.append({
                "type": "notification",
                "text": text,
                "extra": extra or {},
            })
            if len(self._notification_history) > self._max_notifications:
                self._notification_history = self._notification_history[-self._max_notifications:]

        # Invalidate prompt_toolkit toolbar on the UI thread (outside lock).
        if self._app is not None:
            try:
                self._app.invalidate()
            except Exception:
                pass



    def get_notifications(self, count: int = 20) -> list[dict[str, Any]]:
        """Get recent swarm notifications.

        Args:
            count: Max number of notifications to return (default 20).

        Returns:
            List of notification dicts sorted newest-first.
        """
        with self._lock:
            recent = list(self._notification_history[-count:])
        recent.reverse()
        return recent

    # ------------------------------------------------------------------
    # Inbox queue (main thread interface)
    # ------------------------------------------------------------------

    def has_pending(self) -> bool:
        """Return True if there are messages in the inbox for the admin LLM."""
        return not self._inbox.empty()

    def take_pending(self) -> dict[str, Any] | None:
        """Pop and return the next inbox message, or None if empty.

        Non-blocking. Called from the main prompt_toolkit thread.
        """
        try:
            return self._inbox.get_nowait()
        except queue.Empty:
            return None

    def set_app(self, app) -> None:
        """Set the prompt_toolkit Application for toolbar invalidation.

        Called once during swarm startup from the main thread.
        """
        self._app = app

    def stop(self, force: bool = False) -> None:
        """Stop the server and clean up workers.

        Args:
            force: If True, always close the websocket server directly
                even if the event loop is not running.
        """
        self.stop_all(force=force)
        if self._thread:
            self._thread.join(timeout=5)
            # If the thread is still alive, the event loop didn't stop on its
            # own (e.g. wait_closed didn't unblock). Force-stop the loop from
            # inside its own thread and wait again.
            if self._thread.is_alive() and self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(timeout=5)
        if self._loop and not self._loop.is_closed():
            self._loop.close()
