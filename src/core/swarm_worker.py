"""Swarm worker — standalone orchestrator that connects to a swarm server.

A worker is a full AgenticOrchestrator with:
- Isolated ChatManager (no markdown logging, no dream memory)
- Auto-approved edits
- Command approval routed to admin via WebSocket
- Context cleared between tasks

The worker runs in its own process/terminal and connects to the admin's
WebSocket server to receive task assignments and send results back.
"""

import logging
import os
import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from rich.console import Console
from rich.text import Text as RichText
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI

from ui.toolbar_interactions import (
    set_active_interaction,
    clear_active_interaction,
    patch_for_active_prompt,
    SETTING_RESOLVED_SENTINEL,
    COMMAND_CONFIRM_SENTINEL,
)

from core.chat_manager import ChatManager

# Sentinel values for programmatic prompt exits
ADMIN_STOP_SENTINEL = 130    # Admin sent stop_worker or pending work
AGENT_DONE_SENTINEL = 999    # Agent thread completed during inputhook
from core.swarm_client import SwarmClient
from llm.prompts import build_swarm_worker_prompt
from utils.settings import swarm_settings
from ui.banner import display_startup_banner
from ui.safe_console import SafeConsole

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
        self._safe_console = SafeConsole(console)
        self._cached_prompt = None
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
        self._spinner_timer: threading.Timer | None = None
        self._deferred_user_input: str | None = None

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
        elif msg.get("type") == "stop_worker":
            self._inbox.put(InboxItem(source="admin", kind="stop"))
            self._admin_work_pending.set()
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
        from tools.helpers.base import ToolRegistry

        path_str = arguments.get("path_str", "")
        scope_error = _validate_worker_write_scope(path_str, self.repo_root, self._write_scope)
        if scope_error:
            return scope_error

        tool_def = ToolRegistry.get("create_file")
        return tool_def.execute(arguments, context)

    @property
    def orchestrator(self) -> Any:
        """Lazily build the orchestrator."""
        if self.__orchestrator is None:
            from core.agentic import AgenticOrchestrator
            self.__orchestrator = AgenticOrchestrator(
                chat_manager=self.chat_manager,
                repo_root=self.repo_root,
                rg_exe_path=self.rg_exe_path,
                console=self._safe_console,
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
        elif item.source == "local" and item.kind == "command":
            self._run_local_command(item.data)
        elif item.source == "local" and item.kind == "task":
            self._run_local_task(item.data)

    def _run_admin_task(self, task_dispatch: dict) -> None:
        """Execute an admin-assigned task and send completion back to server.

        Runs the orchestrator in a background thread while the main thread
        drives a PTK prompt with live toolbar updates and progress spinner.
        """
        from ui.prompt_utils import get_worker_toolbar_text

        self._busy.set()
        session = self._prompt_session

        try:
            self._clear_terminal_for_next_task()
            self._print_task_header(task_dispatch)

            # Setup task context
            self._task_id = task_dispatch["task_id"]
            self._write_scope = list(task_dispatch.get("write_scope") or [])
            prompt = task_dispatch["prompt"]

            # Bug 3 fix: reset cancellation state from any prior task.
            # clear_agent_cancel() creates a fresh Event so the old (set)
            # event is no longer referenced by chat_manager, but the cached
            # orchestrator still holds its snapshot — refresh it.
            self.chat_manager.clear_agent_cancel()
            if self.__orchestrator is not None:
                self.__orchestrator._cancel_event = self.chat_manager.get_agent_cancel_event()

            # Reset tool-call budget for the new task
            self.orchestrator.tool_calls_count = 0
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

            # Run orchestrator in background thread with spinner
            completion_event = threading.Event()
            result_holder = {'interrupted': False, 'error': None}

            self._start_progress_spinner()
            agent_thread = threading.Thread(
                target=self._run_agent_in_thread,
                args=(prompt, completion_event, result_holder),
                daemon=True,
            )
            agent_thread.start()

            # Main thread runs PTK prompt with live toolbar. If the user sends
            # a line while the agent is running, queue it as the next local
            # prompt and keep waiting for the active task to finish.
            if session:
                self._safe_console.set_app(session.app)
                try:
                    raw_result = self._wait_for_agent_with_prompt(
                        session,
                        completion_event,
                        lambda: get_worker_toolbar_text(self.chat_manager, self),
                    )
                except KeyboardInterrupt:
                    result_holder['interrupted'] = True
                    self._cancel_and_join_agent(completion_event, agent_thread)
                    raw_result = None
            else:
                raw_result = None

            # Bug 2 fix: admin sent stop_worker during active task.
            # The inputhook exits with result=ADMIN_STOP_SENTINEL when
            # _admin_work_pending is set.  Cancel the agent thread and
            # return without sending a completion summary.
            if raw_result == ADMIN_STOP_SENTINEL:
                self._cancel_and_join_agent(completion_event, agent_thread)
                self._stop_progress_spinner()
                return

            self._stop_progress_spinner()

            # Extract final response
            error = result_holder.get('error')
            user_intervened = result_holder.get('interrupted', False)

            if error:
                final_content = f"Error: {error}"
                self.console.print(f"[red]Worker task failed: {error}[/red]", markup=False)
            elif user_intervened:
                final_content = "Task interrupted by user"
                self.console.print("[yellow]Worker interrupted by user (Ctrl+C)[/yellow]")
            else:
                final_content = ""
                for msg in reversed(self.chat_manager.messages):
                    if msg.get("role") == "assistant" and msg.get("content"):
                        final_content = msg["content"].strip()
                        break
                if not final_content:
                    final_content = "Task completed with no output."

            # Send completion summary
            if not self._killed:
                sent = self.send_completion_summary(
                    message=final_content,
                    status="failed" if error else "done",
                    user_intervened=user_intervened,
                )
                if sent:
                    self.console.print("[dim]Completion summary sent.[/dim]")
                else:
                    self.console.print("[red]Failed to send completion summary; swarm connection may be closed.[/red]")
                    self._running = False
        finally:
            self._safe_console.set_app(None)
            self._busy.clear()
            self._drain_stdin()
            self._enqueue_deferred_user_input()

    def _run_local_task(self, prompt: str) -> None:
        """Execute a local task using the same executor as admin tasks.

        Runs the orchestrator in a background thread while the main thread
        drives a PTK prompt with live toolbar. Does not send a completion
        summary back to the server since local tasks don't have a
        server-side task_id.
        """
        from ui.prompt_utils import get_worker_toolbar_text

        self._busy.set()
        session = self._prompt_session

        try:
            self._task_id = f"local-{uuid.uuid4().hex[:8]}"
            self._write_scope = []

            # Bug 3 fix: reset cancellation state from any prior task.
            self.chat_manager.clear_agent_cancel()
            if self.__orchestrator is not None:
                self.__orchestrator._cancel_event = self.chat_manager.get_agent_cancel_event()

            # Reset tool-call budget for the new task (was missing for local tasks)
            self.orchestrator.tool_calls_count = 0

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

            # Run orchestrator in background thread with spinner
            completion_event = threading.Event()
            result_holder = {'interrupted': False, 'error': None}

            self._start_progress_spinner()
            agent_thread = threading.Thread(
                target=self._run_agent_in_thread,
                args=(prompt, completion_event, result_holder),
                daemon=True,
            )
            agent_thread.start()

            # Main thread runs PTK prompt with live toolbar. If the user sends
            # a line while the agent is running, queue it as the next local
            # prompt and keep waiting for the active task to finish.
            if session:
                self._safe_console.set_app(session.app)
                try:
                    raw_result = self._wait_for_agent_with_prompt(
                        session,
                        completion_event,
                        lambda: get_worker_toolbar_text(self.chat_manager, self),
                    )
                except KeyboardInterrupt:
                    result_holder['interrupted'] = True
                    self._cancel_and_join_agent(completion_event, agent_thread)
                    raw_result = None
            else:
                raw_result = None

            # Bug 2 fix: admin sent stop_worker during active task.
            if raw_result == ADMIN_STOP_SENTINEL:
                self._cancel_and_join_agent(completion_event, agent_thread)
                self._stop_progress_spinner()
                return

            self._stop_progress_spinner()

            error = result_holder.get('error')
            user_intervened = result_holder.get('interrupted', False)
            if error:
                self.console.print(f"[red]Local task failed: {error}[/red]", markup=False)
            elif user_intervened:
                self.console.print("[yellow]Worker interrupted by user (Ctrl+C)[/yellow]")

            self.console.print("[dim]Local task finished.[/dim]")
        finally:
            self._safe_console.set_app(None)
            self._busy.clear()
            self._drain_stdin()
            self._enqueue_deferred_user_input()

    def _run_local_command(self, command: str) -> str | None:
        """Execute a local slash command. Returns the command status string."""
        from ui.commands import process_command
        debug_mode = {"debug": False}
        status, _, _ = process_command(self.chat_manager, command, self._safe_console, debug_mode)
        if status == "exit":
            self._running = False
        return status

    def _try_pop_inbox(self, timeout: float = 0.1) -> InboxItem | None:
        """Try to pop the next inbox item.

        Returns None if no item is available within the timeout.
        Admin items are processed before local items.
        """
        try:
            return self._inbox.get(timeout=timeout)
        except queue.Empty:
            return None

    # ── Prompt helpers ──────────────────────────────────────────────

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

    def _get_worker_prompt(self):
        """Return styled prompt caret for worker idle state."""
        if self._cached_prompt is None:
            prompt_text = RichText.assemble((" > ", "white"))
            with self.console.capture() as capture:
                self.console.print(prompt_text, end="")
            self._cached_prompt = ANSI(capture.get())
        return self._cached_prompt

    def _create_worker_inputhook(self):
        """Inputhook that exits prompt when admin work arrives."""
        def inputhook(context):
            while not context.input_is_ready():
                if self._admin_work_pending.is_set() or not self._running:
                    return
                time.sleep(0.05)
        return inputhook

    def _create_worker_pre_run(self):
        """pre_run that creates background task to exit on admin work."""
        import asyncio
        from prompt_toolkit.application import get_app

        def pre_run():
            app = get_app()
            async def poll():
                while True:
                    if self._admin_work_pending.is_set() or not self._running:
                        try:
                            get_app().exit(result=ADMIN_STOP_SENTINEL)
                        except Exception:
                            pass
                        return
                    await asyncio.sleep(0.1)
            app.create_background_task(poll())
        return pre_run

    def _create_agent_done_inputhook(self, completion_event):
        """Inputhook that exits when agent work completes or admin sends stop.

        User keystrokes are silently ignored while the agent is running;
        the prompt will only exit when the agent finishes or a stop is received.
        """
        def inputhook(context):
            while True:
                if completion_event.is_set():
                    from prompt_toolkit.application import get_app
                    get_app().exit(result=AGENT_DONE_SENTINEL)
                    return
                if not self._running:
                    return
                if self._admin_work_pending.is_set():
                    from prompt_toolkit.application import get_app
                    get_app().exit(result=ADMIN_STOP_SENTINEL)  # Exit via sentinel, like idle pre_run
                    return
                if context.input_is_ready():
                    # Let prompt_toolkit consume the key event. The active-task
                    # prompt uses no-op bindings for normal keys, so input is
                    # discarded instead of accepting the hidden prompt. Keeping
                    # this hook in a loop while input is ready can starve PTK's
                    # event processing and pin the CPU.
                    return
                time.sleep(0.05)
        return inputhook

    def _wait_for_agent_with_prompt(self, session, completion_event, toolbar_fn):
        """Keep the active-task prompt alive while queueing user-submitted lines."""
        while True:
            raw_result = session.prompt(
                lambda: "",
                bottom_toolbar=toolbar_fn,
                inputhook=self._create_agent_done_inputhook(completion_event),
            )
            if raw_result == AGENT_DONE_SENTINEL or raw_result == ADMIN_STOP_SENTINEL:
                return raw_result
            if isinstance(raw_result, str) and raw_result.strip():
                self._set_deferred_user_input(raw_result)
            if completion_event.is_set():
                return AGENT_DONE_SENTINEL
            if not self._running or self._admin_work_pending.is_set():
                return ADMIN_STOP_SENTINEL

    def _enqueue_deferred_user_input(self) -> None:
        """Move user text typed during active work into the normal inbox."""
        text = (self._deferred_user_input or "").strip()
        self._deferred_user_input = None
        if not text:
            return
        kind = "command" if text.startswith("/") else "task"
        self._inbox.put(InboxItem(source="local", kind=kind, data=text))

    def _set_deferred_user_input(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self._deferred_user_input:
            self._deferred_user_input = f"{self._deferred_user_input}\n{text}"
        else:
            self._deferred_user_input = text

    # ── Progress spinner ───────────────────────────────────────────

    _SPINNER_REFRESH_INTERVAL = 0.1

    def _start_progress_spinner(self, message=""):
        """Start toolbar spinner and frame-advance timer."""
        progress = getattr(self.chat_manager, 'progress', None)
        if progress:
            progress.start_spinner(message)
            self._schedule_spinner_advance()

    def _schedule_spinner_advance(self):
        """Schedule next spinner frame advance."""
        progress = getattr(self.chat_manager, 'progress', None)
        if progress and (progress.spinner_active or progress.subagent_active):
            progress.advance_spinner()
            self._invalidate_toolbar()
            self._spinner_timer = threading.Timer(
                self._SPINNER_REFRESH_INTERVAL,
                self._schedule_spinner_advance,
            )
            self._spinner_timer.daemon = True
            self._spinner_timer.start()

    def _stop_progress_spinner(self):
        """Stop toolbar spinner and cancel timer."""
        if self._spinner_timer:
            self._spinner_timer.cancel()
            self._spinner_timer = None
        progress = getattr(self.chat_manager, 'progress', None)
        if progress:
            progress.stop_spinner()
        self._invalidate_toolbar()

    def _invalidate_toolbar(self):
        """Trigger toolbar repaint from any thread."""
        session = self._prompt_session
        if session and session.app and session.app.is_running:
            try:
                session.app.invalidate()
            except Exception:
                pass

    # ── Agent-in-thread runner ─────────────────────────────────────

    def _run_agent_in_thread(self, prompt_text, completion_event, result_holder):
        """Run the orchestrator in a background thread.

        The main thread runs a PTK prompt with live toolbar updates
        while this thread executes the agent loop.
        """
        try:
            self.orchestrator.run(
                prompt_text,
                thinking_indicator=None,  # spinner replaces thinking indicator
                allowed_tools=self.worker_tools,
                allow_active_plugins=self.allow_active_plugins,
            )
        except Exception as e:
            result_holder['error'] = str(e)
            logger.error("Worker task error: %s", e)
        finally:
            completion_event.set()

    def _cancel_and_join_agent(self, completion_event, agent_thread,
                                wait_timeout=5.0, join_timeout=10.0):
        """Cancel the running agent and wait for its thread to finish.

        Requests cancellation via the chat manager, then waits for the
        completion event and joins the agent thread with bounded timeouts
        to prevent deadlocks.  Used by both Ctrl+C and admin-stop paths.

        Args:
            completion_event: threading.Event set when the agent finishes.
            agent_thread: The background agent thread.
            wait_timeout: Seconds to wait for completion_event after cancel.
            join_timeout: Seconds to wait for the thread after completion_event.
        """
        self.chat_manager.request_agent_cancel()
        completion_event.wait(timeout=wait_timeout)
        agent_thread.join(timeout=join_timeout)
        if agent_thread.is_alive():
            logger.warning(
                "Agent thread did not terminate within %ss after cancel",
                wait_timeout + join_timeout,
            )

    # ── Main loop ──────────────────────────────────────────────────

    def wait_for_tasks(self) -> None:
        """Main-thread PTK prompt loop with full feature parity to the admin.

        Instead of a background input thread, the main thread owns the
        PromptSession with toolbar, key bindings, styled prompt, and
        agent-working state with progress spinner.
        """
        from ui.prompt_utils import (
            get_worker_toolbar_text, setup_common_bindings, TOOLBAR_STYLE,
        )
        from ui.status_state import ProgressState

        # Ensure ChatManager has progress state for spinner/toolbar
        if not hasattr(self.chat_manager, 'progress') or self.chat_manager.progress is None:
            self.chat_manager.progress = ProgressState()

        # Setup PromptSession with full features
        bindings = setup_common_bindings(self.chat_manager)
        session = PromptSession(key_bindings=bindings, style=TOOLBAR_STYLE)
        self._running = True
        self._prompt_session = session

        # Register toolbar invalidation callback for background thread redraws
        self.chat_manager._invalidate_toolbar = self._invalidate_toolbar

        self.console.print("[dim]Worker ready. Waiting for admin tasks. Press Ctrl+C twice to exit, or /exit.[/dim]")

        last_ctrl_c_time: float = 0.0

        while self._running:
            # Check connection
            if not self._client or not self._client.is_connected:
                self.console.print("[dim]Swarm connection closed — exiting worker.[/dim]")
                break

            # Check for pending admin work before prompting
            if self._admin_work_pending.is_set():
                item = self._try_pop_inbox(timeout=0.01)
                if item:
                    self._process_inbox_item(item)
                else:
                    self._admin_work_pending.clear()
                continue

            # Drain any queued inbox items
            while True:
                item = self._try_pop_inbox(timeout=0.01)
                if item is None:
                    break
                self._process_inbox_item(item)
                if not self._running:
                    break
            if not self._running:
                break

            # Prompt with full PTK features
            try:
                raw_input = session.prompt(
                    lambda: self._get_worker_prompt(),
                    bottom_toolbar=lambda: get_worker_toolbar_text(self.chat_manager, self),
                    inputhook=self._create_worker_inputhook(),
                    pre_run=self._create_worker_pre_run(),
                )
            except KeyboardInterrupt:
                now = time.monotonic()
                if last_ctrl_c_time and (now - last_ctrl_c_time) < 2.0:
                    self._running = False
                    break
                last_ctrl_c_time = now
                self.console.print("[dim]Ctrl+C again within 2s to exit, or /exit.[/dim]")
                continue
            except EOFError:
                continue

            # ADMIN_STOP_SENTINEL from pre_run — admin work arrived during prompt
            if raw_input == ADMIN_STOP_SENTINEL:
                continue

            # Setting selector resolved via toolbar
            if raw_input == SETTING_RESOLVED_SENTINEL and getattr(self.chat_manager, '_setting_selector', None) is not None:
                selector = self.chat_manager._setting_selector
                continuation = getattr(self.chat_manager, '_setting_continuation', None)
                try:
                    if continuation is not None:
                        continuation(selector)
                finally:
                    new_selector = getattr(self.chat_manager, '_setting_selector', None)
                    if new_selector is not selector:
                        patch_for_active_prompt(new_selector, SETTING_RESOLVED_SENTINEL)
                        set_active_interaction(self.chat_manager, new_selector)
                    else:
                        self.chat_manager._setting_selector = None
                        self.chat_manager._setting_continuation = None
                        clear_active_interaction(self.chat_manager)
                continue

            # Command confirm resolved via toolbar
            if raw_input == COMMAND_CONFIRM_SENTINEL and getattr(self.chat_manager, '_confirm_interaction', None) is not None:
                interaction = self.chat_manager._confirm_interaction
                continuation = getattr(self.chat_manager, '_confirm_continuation', None)
                cancelled = interaction.was_cancelled()
                try:
                    if continuation is not None:
                        continuation(None if cancelled else True)
                finally:
                    self.chat_manager._confirm_interaction = None
                    self.chat_manager._confirm_continuation = None
                    clear_active_interaction(self.chat_manager)
                continue

            # Pending text interaction resolved via toolbar
            if raw_input and isinstance(raw_input, str) and getattr(self.chat_manager, '_pending_text_interaction', None) is not None:
                pending = self.chat_manager._pending_text_interaction
                continuation = getattr(self.chat_manager, '_pending_text_continuation', None)
                try:
                    if continuation is not None:
                        continuation(raw_input)
                finally:
                    self.chat_manager._pending_text_interaction = None
                    self.chat_manager._pending_text_continuation = None
                    clear_active_interaction(self.chat_manager)
                continue

            # Process user input
            if raw_input and isinstance(raw_input, str):
                stripped = raw_input.strip()
                if stripped.startswith("/"):
                    cmd_status = self._run_local_command(stripped)
                    if cmd_status == "setting_selector":
                        selector = getattr(self.chat_manager, '_setting_selector', None)
                        if selector is not None:
                            patch_for_active_prompt(selector, SETTING_RESOLVED_SENTINEL)
                            set_active_interaction(self.chat_manager, selector)
                    elif cmd_status == "confirm_input":
                        interaction = getattr(self.chat_manager, '_confirm_interaction', None)
                        if interaction is not None:
                            patch_for_active_prompt(interaction, COMMAND_CONFIRM_SENTINEL)
                            set_active_interaction(self.chat_manager, interaction)
                    elif cmd_status == "text_input":
                        pass  # Already set on chat_manager by handoff
                    elif cmd_status == "subagent_run":
                        self.console.print("[dim]Sub-agent commands not supported in worker mode.[/dim]")
                    elif cmd_status == "handled":
                        pass
                elif stripped:
                    self._run_local_task(stripped)

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
        self._killed = True
        self._stop_progress_spinner()
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
