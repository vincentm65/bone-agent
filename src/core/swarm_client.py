"""Swarm client — WebSocket connection manager for swarm workers.

Handles the low-level WebSocket lifecycle for worker processes:
- Connect to admin server via ws://host:port/<swarm_name>
- Send worker_hello and receive server-assigned worker_id
- Route messages via async WebSocket loop and on_message callback
- Send task_started, completion_summary, approval_request
- Receive approval_response, task_dispatch, stop_worker, ping

Designed as a standalone module so it can be reused by the worker runner
(swarm_worker.py) or any other swarm client implementation.

No reconnect logic in v1. Connection failure at startup prints an error
and exits non-zero. Workers do not continue to the next task after
server disconnect.
"""

import asyncio
import json
import logging
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class SwarmClient:
    """WebSocket client for a single swarm worker connection.

    The WebSocket connection and message loop run in a background thread.
    The main thread sends via ``send()`` and receives via the ``on_message`` callback.
    """

    def __init__(
        self,
        swarm_name: str,
        host: str = "127.0.0.1",
        port: int = 8765,
        on_worker_id: Optional[Callable[[str], None]] = None,
        on_message: Optional[Callable[[dict], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
    ):
        """Initialize the swarm client.

        Args:
            swarm_name: Name of the swarm to join.
            host: WebSocket server host.
            port: WebSocket server port.
            on_worker_id: Callback fired when worker_id is assigned.
            on_message: Callback fired for each incoming message (before queueing).
            on_disconnect: Callback fired when connection is lost.
        """
        self.swarm_name = swarm_name
        self.host = host
        self.port = port
        self.base_url = f"ws://{host}:{port}/{swarm_name}"

        self._on_worker_id = on_worker_id
        self._on_message = on_message
        self._on_disconnect = on_disconnect

        self._worker_id: str = "unknown"
        self._running = False
        self._ws: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    @property
    def worker_id(self) -> str:
        """The server-assigned worker ID, or 'unknown' if not yet assigned."""
        return self._worker_id

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._running

    def connect(self) -> bool:
        """Connect to the swarm server and perform the hello handshake.

        Connects synchronously, sends worker_hello, receives worker_id.
        Then leaves the background message loop running.

        Returns:
            True if connection successful, False otherwise.
        """
        ready = threading.Event()
        error: list[Exception] = []

        self._loop = asyncio.new_event_loop()
        self._running = True
        self._thread = threading.Thread(
            target=self._run_message_loop,
            args=(ready, error),
            daemon=True,
        )
        self._thread.start()

        if not ready.wait(timeout=10):
            self._running = False
            logger.error("Timed out connecting to swarm server")
            return False

        if error:
            self._running = False
            logger.error("Failed to connect to swarm server: %s", error[0])
            return False

        return True

    def _run_message_loop(
        self,
        ready: threading.Event | None = None,
        error: list[Exception] | None = None,
    ) -> None:
        """Background thread: own the WebSocket connection and route messages."""
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._message_loop(ready, error))
        finally:
            self._loop.close()

    async def _message_loop(
        self,
        ready: threading.Event | None,
        error: list[Exception] | None,
    ) -> None:
        """Async WebSocket lifecycle for the worker connection."""
        try:
            import websockets

            async with websockets.connect(self.base_url) as ws:
                self._ws = ws
                await ws.send(json.dumps({"type": "worker_hello"}))
                response = await ws.recv()
                resp = json.loads(response)
                self._worker_id = resp.get("worker_id", "unknown")

                if self._on_worker_id:
                    self._on_worker_id(self._worker_id)

                if ready:
                    ready.set()

                while self._running:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        await ws.ping()
                        continue

                    msg = json.loads(raw)
                    msg_type = msg.get("type")

                    if msg_type == "approval_response":
                        # Notify the generic message callback; worker approval
                        # handling uses this path to resolve blocking Futures.
                        if self._on_message:
                            self._on_message(msg)
                    elif msg_type in {"task_dispatch", "stop_worker"}:
                        if self._on_message:
                            self._on_message(msg)
                        if msg_type == "stop_worker":
                            self._running = False
                    elif msg_type == "ping":
                        pass  # heartbeat — no action needed
                    else:
                        # Other server notices — forward to callback.
                        if self._on_message:
                            self._on_message(msg)

        except Exception as e:
            if ready and not ready.is_set():
                if error is not None:
                    error.append(e)
                ready.set()
            else:
                # Suppress expected disconnect errors — these are normal when
                # the admin shuts down the swarm or the connection drops.
                import websockets.exceptions
                if isinstance(e, (websockets.exceptions.ConnectionClosed, ConnectionResetError, ConnectionAbortedError, ConnectionError)):
                    logger.debug("WebSocket disconnected: %s", e)
                else:
                    logger.warning("Client message loop error: %s", e)
        finally:
            self._ws = None
            self._running = False
            # Connection lost
            if self._on_disconnect:
                self._on_disconnect()

    def send(self, message: dict) -> bool:
        """Send a message to the server (thread-safe via async loop).

        Args:
            message: Dict to serialize as JSON.

        Returns:
            True if sent successfully, False otherwise.
        """
        try:
            if not self._loop or not self._loop.is_running():
                logger.warning("Cannot send — event loop not running")
                return False
            asyncio.run_coroutine_threadsafe(
                self._ws.send(json.dumps(message)),
                self._loop,
            ).result(timeout=10)
            return True
        except Exception as e:
            logger.warning("Failed to send message: %s", e)
            return False

    def shutdown(self) -> None:
        """Close the WebSocket connection and stop the message loop."""
        self._running = False
        if self._ws:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._ws.close(), self._loop
                ).result(timeout=5)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
