"""Swarm worker — standalone orchestrator that connects to a swarm server.

A worker is a full AgenticOrchestrator with:
- Isolated ChatManager (no markdown logging, no dream memory)
- Auto-approved edits
- Command approval routed to admin via WebSocket
- Context cleared between tasks

The worker runs in its own process/terminal and connects to the admin's
WebSocket server to receive task assignments and send results back.
"""

import json
import logging
import os
import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from ui.thinking import ThinkingIndicator
from typing import Any, Optional

from rich.console import Console
from prompt_toolkit import PromptSession

from core.chat_manager import ChatManager
from core.swarm_client import SwarmClient
from llm.prompts import build_swarm_worker_prompt
from utils.settings import swarm_settings
from ui.banner import display_startup_banner

logger = logging.getLogger(__name__)


# ── Inbox item types ─────────────────────────────────────────────────

@dataclass(frozen=True)
class InboxItem:
    """Internal inbox item — the single message shape for all worker input."""
    source: str          # "admin" or "local"
    kind: str            # "task", "command", or "stop"
    data: Any = None     # task_dispatch dict, command string, or None


# ── ChatManager factory ──────────────────────────────────────────────


def _create_worker_chat_manager(system_prompt: str) -> ChatManager:
    """Create a fresh ChatManager for a swarm worker.

    Disables markdown logging and user message logging to prevent
    memory contributions and conversation log pollution.

    Args:
        system_prompt: Pre-built worker system prompt.

    Returns:
        Configured ChatManager instance.
    """
    cm = ChatManager(compact_trigger_tokens=None)
    cm._compaction_disabled = True
    cm.markdown_logger = None
    cm.user_message_logger = None
    # Replace system prompt (ChatManager builds its own on init)
    cm.messages = [{"role": "system", "content": system_prompt}]
    cm._update_context_tokens()
    cm.context_token_estimate = cm.token_tracker.current_context_tokens
    return cm


def clear_worker_context(chat_manager: ChatManager) -> None:
    """Clear a worker's message history, keeping only the system prompt.

    Called before each new task assignment. No teardown or recreation —
    the ChatManager, tool tracker, and client are all preserved.

    Args:
        chat_manager: Worker's ChatManager instance.
    """
    system_prompt = chat_manager.messages[0]["content"] if chat_manager.messages else ""
    chat_manager.messages = [{"role": "system", "content": system_prompt}]
    chat_manager._update_context_tokens()
    chat_manager.context_token_estimate = chat_manager.token_tracker.current_context_tokens
    chat_manager.task_list.clear()
    chat_manager.task_list_title = None


# ── Approval handling ────────────────────────────────────────────────

import threading as _threading
import concurrent.futures as _futures


class ApprovalAwaiter:
    """Thread-safe awaiter for command approval responses.

    The worker's main WebSocket connection handles both task dispatches
    and approval responses. This class provides a synchronous wait
    interface for command approval callbacks.
    """

    def __init__(self):
        self._lock = _threading.Lock()
        self._futures: dict[str, _futures.Future] = {}

    def submit(self, call_id: str) -> _futures.Future:
        """Register a pending approval request.

        Returns a Future that will be resolved when the approval response arrives.
        """
        future = _futures.Future()
        with self._lock:
            self._futures[call_id] = future
        return future

    def resolve(self, call_id: str, approved: bool, guidance: str = "") -> None:
        """Resolve a pending approval request.

        Called from the WebSocket message loop when an approval_response arrives.
        """
        with self._lock:
            future = self._futures.pop(call_id, None)
        if future and not future.done():
            future.set_result({"approved": approved, "guidance": guidance})

    def cleanup(self) -> None:
        """Cancel all pending futures (called on shutdown)."""
        with self._lock:
            for f in self._futures.values():
                if not f.done():
                    f.cancel()
            self._futures.clear()


# ── Worker edit approval handler ─────────────────────────────────────

def _worker_handle_edit_approval(args_dict: dict, repo_root: str,
                                  console: Console, gitignore_spec: Any,
                                  vault_root_str: Any,
                                  write_scope: list[str] | None = None) -> tuple[str, bool]:
    """Auto-approve edit for a worker.

    Executes the edit directly without prompting. Returns the same
    (result_str, should_exit) tuple as handle_edit_approval.

    Args:
        args_dict: Tool arguments (path, search, replace, context_lines, reason).
        repo_root: Repository root path.
        console: Rich console for output.
        gitignore_spec: Gitignore spec for path filtering.
        vault_root_str: Callable returning vault root path.
        write_scope: Optional list of paths the worker may edit/create.

    Returns:
        (result_str, should_exit) tuple — should_exit is always False for auto-approve.
    """
    from tools.edit import _execute_edit_file, preview_edit_file

    scope_error = _validate_worker_write_scope(args_dict.get('path', ''), repo_root, write_scope)
    if scope_error:
        return scope_error, False

    try:
        preview_status, preview = preview_edit_file(
            args_dict,
            repo_root,
            gitignore_spec,
            vault_root=vault_root_str(),
        )
    except Exception as e:
        return f"exit_code=1\nEdit failed: {str(e)}", False

    if preview_status != "exit_code=0":
        return preview_status, False

    if console:
        console.print(preview)
        console.print()

    final_result = _execute_edit_file(
        path=args_dict.get('path'),
        search=args_dict.get('search'),
        replace=args_dict.get('replace'),
        repo_root=repo_root,
        console=console,
        gitignore_spec=gitignore_spec,
        context_lines=args_dict.get('context_lines', 3),
        vault_root=vault_root_str()
    )

    # Strip exit_code line from final result before returning
    if final_result and isinstance(final_result, str):
        result_lines = [line for line in final_result.split('\n') if not line.startswith('exit_code=')]
        final_result = '\n'.join(result_lines).strip()

    return final_result, False


def _validate_worker_write_scope(path: str, repo_root: str, write_scope: list[str] | None) -> str | None:
    """Return an error when a worker edit is outside its delegated write scope."""
    if not write_scope:
        return None

    try:
        repo_path = Path(repo_root).resolve()
        target_path = Path(path)
        if not target_path.is_absolute():
            target_path = repo_path / target_path
        target_path = target_path.resolve()

        for scope_item in write_scope:
            allowed_path = Path(scope_item)
            if not allowed_path.is_absolute():
                allowed_path = repo_path / allowed_path
            allowed_path = allowed_path.resolve()

            if target_path == allowed_path or allowed_path in target_path.parents:
                return None
    except Exception as e:
        return f"exit_code=1\nEdit blocked: could not validate write scope for '{path}': {e}"

    allowed = ", ".join(write_scope)
    return (
        "exit_code=1\n"
        f"Edit blocked: '{path}' is outside this task's write scope.\n"
        f"Allowed write scope: {allowed}\n"
        "Ask the admin to delegate this file before editing it."
    )


# ── Worker command approval handler ──────────────────────────────────


def _worker_handle_command_approval(
    command: str,
    arguments: dict,
    tool: Any,
    context: dict,
    console: Console | None,
    debug_mode: bool,
    approve_awaiter: ApprovalAwaiter,
    send_approval_fn: Any,
    task_id: str,
    worker_id: str,
    approval_timeout: int = 300,
    cron_job_id: Optional[str] = None,
    cron_allowlist: Any = None,
    cron_interactive: bool = False,
) -> tuple[str, bool, bool]:
    """Handle command approval for a worker.

    Checks for silent blocks and auto-approval first (same as main agent).
    For non-auto-approved commands, routes through the approval awaiter.

    Args:
        command: The shell command string.
        arguments: Tool arguments dict.
        tool: The tool object to execute on approval.
        context: Tool execution context dict.
        console: Rich console for output.
        debug_mode: Whether debug mode is active.
        approve_awaiter: Thread-safe approval response awaiter.
        send_approval_fn: Callable that sends approval_request to the server.
        task_id: Current task ID.
        worker_id: Worker ID.
        approval_timeout: Timeout in seconds.
        cron_job_id: Optional cron job ID.
        cron_allowlist: Optional cron allowlist.
        cron_interactive: If True, cron job is in interactive test-run mode.

    Returns:
        (result, should_exit, command_executed) tuple.
    """
    from utils.validation import is_auto_approved_command, check_for_silent_blocked_command

    # Check for silent blocks
    is_blocked, reprompt_msg = check_for_silent_blocked_command(command)
    if is_blocked:
        if debug_mode:
            console.print(f"[dim]Silently blocked command: {command.split()[0]}[/dim]")
        return f"exit_code=1\n{reprompt_msg}", False, False

    # Check auto-approval (global safe commands)
    auto_approve = is_auto_approved_command(command)

    # Check cron allow list
    cron_auto_approved = False
    if cron_job_id and cron_allowlist:
        if cron_allowlist.is_allowed(cron_job_id, command):
            cron_auto_approved = True
        elif not auto_approve:
            if cron_interactive:
                pass  # Fall through to interactive approval
            else:
                allowed_cmds = cron_allowlist.get_commands(cron_job_id)
                allowed_preview = ", ".join(f"'{c}'" for c in allowed_cmds[:5])
                if len(allowed_cmds) > 5:
                    allowed_preview += f", ... ({len(allowed_cmds)} total)"
                if not allowed_preview:
                    allowed_preview = "(none)"
                return (
                    f"exit_code=1\n"
                    f"Command not in cron allow list for job '{cron_job_id}'.\n"
                    f"Command: {command}\n"
                    f"Allowed: {allowed_preview}\n"
                    f"Do not retry this command."
                ), False, False

    if cron_auto_approved or auto_approve:
        result = tool.execute(arguments, context)
        command_executed = True
        return result, False, command_executed

    # Route to admin via WebSocket
    call_id = f"cmd-{uuid.uuid4().hex[:6]}"
    if console:
        console.print(f"[yellow]Requesting approval for {command}[/yellow]")

    # Register before sending so a fast approval response cannot race past us.
    future = approve_awaiter.submit(call_id)

    # Submit approval request to server
    sent = send_approval_fn(
        task_id=task_id,
        worker_id=worker_id,
        call_id=call_id,
        command=command,
        reason=arguments.get('reason', 'Execute shell command'),
        cwd=arguments.get('cwd', ''),
        preview="",
    )
    if sent is False:
        approve_awaiter.resolve(call_id, False, "Could not reach admin for approval")
        result = {"approved": False, "guidance": "Could not reach admin for approval"}
    else:
        result = None

    # Wait for approval response
    if result is None:
        try:
            result = future.result(timeout=approval_timeout)
        except _futures.TimeoutError:
            approve_awaiter.resolve(call_id, False, f"Approval timeout ({approval_timeout}s)")
            send_approval_fn(
                type="approval_cancelled",
                task_id=task_id,
                worker_id=worker_id,
                call_id=call_id,
                reason=f"Approval timeout ({approval_timeout}s)",
            )
            result = {"approved": False, "guidance": f"Approval timeout ({approval_timeout}s)"}

    if result["approved"]:
        exec_result = tool.execute(arguments, context)
        command_executed = True
        guidance = str(result.get("guidance") or "").strip()
        if guidance:
            if console:
                console.print(f"[dim]Admin guidance: {guidance}[/dim]")
            exec_result = (
                f"[Admin guidance: {guidance}]\n\n"
                f"{exec_result}"
            )
        return exec_result, False, command_executed
    else:
        guidance = result.get("guidance", "Command denied")
        if console:
            console.print(f"[red]Command denied by admin: {guidance}[/red]")
        return (
            f"exit_code=1\n"
            f"Command denied by admin.\n"
            f"Reason: {guidance}\n"
            f"Revise your approach and try again with a different command."
        ), False, False


# ── Worker runner ────────────────────────────────────────────────────


class SwarmWorkerRunner:
    """Owns a single worker's lifecycle: WebSocket client, ChatManager, Orchestrator.

    Receives task assignments, executes them with full tool access,
    and sends completion summaries back to the admin server.

    Uses SwarmClient for the WebSocket connection.
    """

    def __init__(
        self,
        swarm_name: str,
        repo_root: str,
        rg_exe_path: str,
        console: Console,
        websocket_url: str = "ws://127.0.0.1",
        approval_timeout: int = 300,
        worker_tools: list[str] | None = None,
        allow_active_plugins: bool = False,
        cron_job_id: Optional[str] = None,
        cron_allowlist: Any = None,
        cron_interactive: bool = False,
    ):
        self.swarm_name = swarm_name
        self.repo_root = Path(repo_root)
        self.rg_exe_path = rg_exe_path
        self.console = console
        self.websocket_url = websocket_url
        self.approval_timeout = approval_timeout
        self.worker_tools = worker_tools or swarm_settings.worker_tools
        self.allow_active_plugins = allow_active_plugins
        self.cron_job_id = cron_job_id
        self.cron_allowlist = cron_allowlist
        self.cron_interactive = cron_interactive

        self._approval_awaiter: ApprovalAwaiter = ApprovalAwaiter()
        self._chat_manager: ChatManager | None = None
        self.__orchestrator: Any | None = None
        self._client: Any = None
        self._task_id: str = ""
        self._write_scope: list[str] = []
        self._running = False
        self._killed = False
        self._prompt_session: PromptSession | None = None
        self._inbox: queue.Queue[InboxItem] = queue.Queue()
        self._admin_work_pending = threading.Event()
        self._busy = threading.Event()
        self._local_input_stop = threading.Event()
        self._local_input_thread: threading.Thread | None = None
        self._local_prompt_active = threading.Event()

    @property
    def worker_id(self) -> str:
        return self._client.worker_id if self._client else "unknown"

    def connect(self) -> bool:
        """Connect to the swarm server via SwarmClient.

        Returns:
            True if connection successful, False otherwise.
        """
        try:
            host, port = self._parse_websocket_url()
            self._client = SwarmClient(
                swarm_name=self.swarm_name,
                host=host,
                port=port,
                on_message=self._handle_client_message,
            )
            if self._client.connect():
                self.console.print(f"[dim]Connected as {self.worker_id}[/dim]")
                return True
            else:
                self.console.print(f"[red]Failed to connect to swarm server[/red]")
                return False
        except Exception as e:
            self.console.print(f"[red]Failed to connect to swarm server: {e}[/red]")
            return False

    def _build_chat_manager(self) -> ChatManager:
        """Build a worker ChatManager with the worker prompt."""
        prompt = build_swarm_worker_prompt()
        return _create_worker_chat_manager(prompt)

    def _parse_websocket_url(self) -> tuple[str, int]:
        """Extract host and port from websocket_url."""
        from urllib.parse import urlparse

        parsed = urlparse(self.websocket_url)
        return parsed.hostname or "127.0.0.1", parsed.port or 8765

    def _handle_client_message(self, msg: dict) -> None:
        """Route async client messages that unblock synchronous worker code.

        Approval responses resolve pending futures.
        Admin messages (task_dispatch, stop_worker) are pushed into the
        unified inbox so they are processed in the main loop.
        """
        if msg.get("type") == "approval_response":
            self._approval_awaiter.resolve(
                msg.get("call_id", ""),
                bool(msg.get("approved")),
                msg.get("guidance", ""),
            )
        elif msg.get("type") == "task_dispatch":
            self._inbox.put(InboxItem(source="admin", kind="task", data=msg))
            self._admin_work_pending.set()
            self._interrupt_idle_prompt()
        elif msg.get("type") == "stop_worker":
            self._inbox.put(InboxItem(source="admin", kind="stop"))
            self._admin_work_pending.set()
            self._interrupt_idle_prompt()
        elif msg.get("type") == "clear_worker_context":
            # Only clear if worker is idle (not running a task)
            if not self._busy.is_set():
                clear_worker_context(self.chat_manager)
                self._client.send({
                    "type": "admin_notice",
                    "worker_id": self.worker_id,
                    "message": "Context cleared by admin",
                })
                self.console.print("[dim]Context cleared by admin request.[/dim]")
            else:
                self._client.send({
                    "type": "admin_notice",
                    "worker_id": self.worker_id,
                    "message": "Cannot clear context: worker is busy",
                })

    @property
    def chat_manager(self) -> ChatManager:
        if self._chat_manager is None:
            self._chat_manager = self._build_chat_manager()
        return self._chat_manager

    def _handle_edit_approval(self, args_dict: dict, repo_root: str,
                              console: Console, gitignore_spec: Any,
                              vault_root_str: Any) -> tuple[str, bool]:
        """Auto-approve worker edits after enforcing current task write scope."""
        return _worker_handle_edit_approval(
            args_dict,
            repo_root,
            console,
            gitignore_spec,
            vault_root_str,
            self._write_scope,
        )

    def _handle_create_file(self, arguments: dict, context: dict) -> str:
        """Create worker files only inside the current task write scope."""
        from tools.create_file import create_file

        path_str = arguments.get("path_str", "")
        scope_error = _validate_worker_write_scope(path_str, self.repo_root, self._write_scope)
        if scope_error:
            return scope_error

        return create_file.execute(arguments, context)

    @property
    def orchestrator(self) -> Any:
        """Lazily build the orchestrator."""
        if self.__orchestrator is None:
            from core.agentic import AgenticOrchestrator
            self.__orchestrator = AgenticOrchestrator(
                chat_manager=self.chat_manager,
                repo_root=self.repo_root,
                rg_exe_path=self.rg_exe_path,
                console=self.console,
                debug_mode=False,
                suppress_result_display=False,
                is_sub_agent=False,
                force_parallel_execution=True,
                cron_job_id=self.cron_job_id,
                cron_allowlist=self.cron_allowlist,
                cron_interactive=self.cron_interactive,
                edit_approval_handler=self._handle_edit_approval,
                create_file_handler=self._handle_create_file,
                command_approval_handler=_worker_handle_command_approval,
                command_approval_awaiter=self._approval_awaiter,
                command_send_approval_fn=self._send_approval_request,
                approval_timeout=self.approval_timeout,
            )
        return self.__orchestrator

    def _clear_terminal_for_next_task(self) -> None:
        """Clear the terminal and display the startup banner (same as /clear)."""
        if hasattr(self.chat_manager, "approve_mode"):
            display_startup_banner(self.chat_manager.approve_mode, clear_screen=True)
        elif sys.stdout.isatty():
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()

    def _print_task_header(self, task_dispatch: dict) -> None:
        """Show the received task prompt before agent execution starts."""
        task_id = task_dispatch.get("task_id", "-")
        prompt = task_dispatch.get("prompt", "")
        write_scope = task_dispatch.get("write_scope") or []

        self.console.print()
        self.console.print(f"[bold #5F9EA0]Swarm task {task_id}[/bold #5F9EA0]")
        if write_scope:
            self.console.print("[dim]Write scope:[/dim]")
            for path in write_scope:
                self.console.print(f"  {path}")
        self.console.print("[dim]Delegated prompt:[/dim]")
        self.console.print(prompt, markup=False)
        self.console.print()

    def send_completion_summary(self, message: str, status: str = "done",
                                 user_intervened: bool = False) -> bool:
        """Send a completion summary to the admin server via SwarmClient."""
        summary = {
            "type": "completion_summary",
            "task_id": self._task_id,
            "worker_id": self.worker_id,
            "message": message,
            "status": status,
            "user_intervened": user_intervened,
        }
        return self._client.send(summary)

    def run_task(self, task_dispatch: dict) -> dict:
        """Execute a single task assignment.

        Args:
            task_dispatch: Dict with 'task_id' and 'prompt' fields.

        Returns:
            Dict with 'task_id', 'summary', 'status'.
        """
        self._task_id = task_dispatch["task_id"]
        self._write_scope = list(task_dispatch.get("write_scope") or [])
        prompt = task_dispatch["prompt"]

        # Reset tool-call budget for the new task
        self.orchestrator.tool_calls_count = 0

        # Set task context for approval routing
        self.orchestrator._current_task_id = self._task_id
        self.orchestrator._current_worker_id = self.worker_id

        # Clear context for the new task
        clear_worker_context(self.chat_manager)

        # Send task_started to server
        self._client.send({
            "type": "task_started",
            "task_id": self._task_id,
            "worker_id": self.worker_id,
        })

        self.console.print()

        # Run the orchestrator loop with spinner
        thinking_indicator = ThinkingIndicator(self.console)
        user_intervened = False
        try:
            thinking_indicator.start()
            self.orchestrator.run(
                prompt,
                thinking_indicator=thinking_indicator,
                allowed_tools=self.worker_tools,
                allow_active_plugins=self.allow_active_plugins,
            )
        except KeyboardInterrupt:
            user_intervened = True
            self.console.print("[yellow]Worker interrupted by user (Ctrl+C)[/yellow]")
        except Exception as e:
            logger.error("Worker task error: %s", e)
            self.console.print(f"[red]Worker task failed before completion summary: {e}[/red]", markup=False)
            return {
                "task_id": self._task_id,
                "summary": f"Error: {e}",
                "status": "failed",
            }
        finally:
            thinking_indicator.stop(reset=True)

        # Extract final response
        final_content = ""
        for msg in reversed(self.chat_manager.messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                final_content = msg["content"].strip()
                break

        if not final_content:
            final_content = "Task completed with no output."

        return {
            "task_id": self._task_id,
            "summary": final_content,
            "status": "done",
            "user_intervened": user_intervened,
        }

    def _process_inbox_item(self, item: InboxItem) -> None:
        """Process the next item from the unified inbox."""
        if item.source == "admin" and item.kind == "task":
            self._admin_work_pending.clear()
            self._run_admin_task(item.data)
        elif item.source == "admin" and item.kind == "stop":
            self._admin_work_pending.clear()
            self._killed = True
            self.console.print("[dim]Received stop_worker — exiting.[/dim]")
            self._running = False
            self._local_input_stop.set()
        elif item.source == "local" and item.kind == "command":
            self._run_local_command(item.data)
        elif item.source == "local" and item.kind == "task":
            self._run_local_task(item.data)

    def _run_admin_task(self, task_dispatch: dict) -> None:
        """Execute an admin-assigned task and send completion back to server."""
        self._busy.set()
        self._interrupt_idle_prompt()
        try:
            self._clear_terminal_for_next_task()
            self._print_task_header(task_dispatch)
            result = self.run_task(task_dispatch)
            if not self._killed:
                sent = self.send_completion_summary(
                    message=result["summary"],
                    status=result["status"],
                    user_intervened=result.get("user_intervened", False),
                )
                if sent:
                    self.console.print("[dim]Completion summary sent.[/dim]")
                else:
                    self.console.print("[red]Failed to send completion summary; swarm connection may be closed.[/red]")
                    self._running = False
                    self._local_input_stop.set()
        finally:
            self._busy.clear()
            self._drain_stdin()

    def _run_local_task(self, prompt: str) -> None:
        """Execute a local task using the same executor as admin tasks.

        Does not send a completion summary back to the server since
        local tasks don't have a server-side task_id.
        """
        self._busy.set()
        try:
            self._task_id = f"local-{uuid.uuid4().hex[:8]}"
            self._write_scope = []

            # Set task context for approval routing
            self.orchestrator._current_task_id = self._task_id
            self.orchestrator._current_worker_id = self.worker_id

            # Clear context for the new task
            clear_worker_context(self.chat_manager)
            display_startup_banner(self.chat_manager.approve_mode, clear_screen=True)

            self.console.print()
            self.console.print(f"[bold #5F9EA0]Local task[/bold #5F9EA0]")
            self.console.print("[dim]Delegated prompt:[/dim]")
            self.console.print(prompt, markup=False)
            self.console.print()

            thinking_indicator = ThinkingIndicator(self.console)
            user_intervened = False
            try:
                thinking_indicator.start()
                self.orchestrator.run(
                    prompt,
                    thinking_indicator=thinking_indicator,
                    allowed_tools=self.worker_tools,
                    allow_active_plugins=self.allow_active_plugins,
                )
            except KeyboardInterrupt:
                user_intervened = True
                self.console.print("[yellow]Worker interrupted by user (Ctrl+C)[/yellow]")
            except Exception as e:
                logger.error("Local task error: %s", e)
                self.console.print(f"[red]Local task failed: {e}[/red]", markup=False)
            finally:
                thinking_indicator.stop(reset=True)
                self.console.print("[dim]Local task finished.[/dim]")
        finally:
            self._busy.clear()
            self._drain_stdin()

    def _run_local_command(self, command: str) -> None:
        """Execute a local slash command."""
        from ui.commands import process_command
        debug_mode = {"debug": False}
        status, _ = process_command(self.chat_manager, command, self.console, debug_mode)
        if status == "exit":
            self._running = False

    def _try_pop_inbox(self, timeout: float = 0.1) -> InboxItem | None:
        """Try to pop the next inbox item.

        Returns None if no item is available within the timeout.
        Admin items are processed before local items.
        """
        try:
            return self._inbox.get(timeout=timeout)
        except queue.Empty:
            return None

    def _start_local_input_thread(self) -> None:
        """Start the idle local-input loop once."""
        if self._local_input_thread and self._local_input_thread.is_alive():
            return
        self._local_input_stop.clear()
        self._local_input_thread = threading.Thread(
            target=self._local_input_loop,
            name="swarm-worker-local-input",
            daemon=True,
        )
        self._local_input_thread.start()

    def _drain_stdin(self):
        """Drain buffered keystrokes and clear the prompt_toolkit buffer."""
        try:
            if os.name != 'nt':
                import termios
                termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
            else:
                import msvcrt
                while msvcrt.kbhit():
                    msvcrt.getch()
        except Exception:
            pass

        try:
            session = self._prompt_session
            if session:
                buf = session.default_buffer
                if buf and buf.text:
                    buf.text = ""
        except Exception:
            pass

    def _local_input_loop(self) -> None:
        """Convert local prompt input into inbox items without owning task execution."""
        idle_notice_shown = False
        while not self._local_input_stop.is_set():
            if not self._running:
                break
            if self._busy.is_set() or self._admin_work_pending.is_set():
                idle_notice_shown = False
                time.sleep(0.05)
                continue

            if not idle_notice_shown:
                self.console.print("[dim]Worker ready. Waiting for admin tasks. Use /exit to leave.[/dim]")
                idle_notice_shown = True

            try:
                self._local_prompt_active.set()
                user_input = self._prompt_session.prompt(" > ", in_thread=True)
            except EOFError:
                continue
            except KeyboardInterrupt:
                self.console.print("[dim]Use /exit to stop this worker.[/dim]")
                continue
            finally:
                self._local_prompt_active.clear()

            if self._local_input_stop.is_set() or self._busy.is_set() or self._admin_work_pending.is_set():
                continue

            stripped = user_input.strip()
            if not stripped:
                continue

            if stripped.startswith("/"):
                self._inbox.put(InboxItem(source="local", kind="command", data=stripped))
            else:
                self._inbox.put(InboxItem(source="local", kind="task", data=stripped))

    def _interrupt_idle_prompt(self) -> None:
        """Best-effort wakeup of the idle prompt.

        Incoming admin messages set the admin_work_pending Event before
        calling this. The prompt exit call is cosmetic and not required
        for dispatch correctness.
        """
        session = self._prompt_session
        app = getattr(session, "app", None) if session else None
        loop = getattr(app, "loop", None) if app else None
        if app and loop:
            try:
                loop.call_soon_threadsafe(app.exit, exception=EOFError)
            except Exception:
                pass

    def wait_for_tasks(self) -> None:
        """Inbox-driven worker loop; local prompt input is only another source."""
        self._running = True
        self._prompt_session = PromptSession()
        self._start_local_input_thread()

        while self._running:
            item = self._try_pop_inbox(timeout=0.1)
            if item is not None:
                self._process_inbox_item(item)
                continue

            if not self._client or not self._client.is_connected:
                self.console.print("[dim]Swarm connection closed — exiting worker.[/dim]")
                self._running = False
                self._local_input_stop.set()
                self._interrupt_idle_prompt()
                return

    def _send_approval_request(self, **kwargs) -> bool:
        """Send an approval_request to the admin server via SwarmClient."""
        message_type = kwargs.pop("type", "approval_request")
        return self._client.send({
            "type": message_type,
            **kwargs,
        })

    def shutdown(self) -> None:
        """Clean up the worker's connections."""
        self._running = False
        self._local_input_stop.set()
        self._interrupt_idle_prompt()
        self._approval_awaiter.cleanup()
        if self._client:
            self._client.shutdown()


# ── CLI entry point ──────────────────────────────────────────────────


def run_worker_cli(
    swarm_name: str,
    repo_root: str,
    rg_exe_path: str,
    host: str = "127.0.0.1",
    port: int = 8765,
    approval_timeout: int = 300,
    worker_tools: list[str] | None = None,
    allow_active_plugins: bool = False,
):
    """Run the worker as a CLI process.

    Connects to the swarm server, waits for task assignments, and
    executes them with full tool access.

    Args:
        swarm_name: Name of the swarm to join.
        repo_root: Repository root path.
        rg_exe_path: Path to rg executable.
        host: WebSocket server host.
        port: WebSocket server port.
        approval_timeout: Timeout for command approval in seconds.
        worker_tools: Override list of allowed tool names.
        allow_active_plugins: Whether to allow active plugin tools.
    """
    console = Console()

    worker = SwarmWorkerRunner(
        swarm_name=swarm_name,
        repo_root=repo_root,
        rg_exe_path=rg_exe_path,
        console=console,
        websocket_url=f"ws://{host}:{port}",
        approval_timeout=approval_timeout,
        worker_tools=worker_tools,
        allow_active_plugins=allow_active_plugins,
    )

    if not worker.connect():
        sys.exit(1)

    try:
        worker.wait_for_tasks()
    except KeyboardInterrupt:
        console.print("\n[dim]Worker interrupted.[/dim]")
    finally:
        worker.shutdown()
        console.print("[dim]Worker exiting.[/dim]")
