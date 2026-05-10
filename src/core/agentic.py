"""Agent tool-calling loop."""

import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

from rich.markdown import Markdown
from rich.text import Text

from utils.settings import MAX_TOOL_CALLS, MonokaiDarkBGStyle, left_align_headings
from tools import (
    read_file,
    list_directory,
    create_file,
    TOOLS,
)
from tools.swarm import ADMIN_SWARM_TOOL_NAMES
from utils.settings import tool_settings

from llm.config import get_provider_config
from utils.result_parsers import extract_exit_code
from utils.terminal_sanitize import sanitize_message_content
from core.retry import (
    RETRY_MAX_ATTEMPTS,
    RETRY_DELAYS,
    is_retryable_error,
    wait_with_cancel_message,
)
from core.tool_approval import (
    handle_edit_approval,
    handle_command_approval,
    resolve_edit_preview,
)
from exceptions import (
    LLMError,
    LLMResponseError,
)


class AgentTurnCancelled(Exception):
    """Raised when the current agent turn is cancelled by the user."""
    pass


class ToolApprovalSuspended(Exception):
    """Raised when a tool needs user approval and yields to the main prompt.

    Carries all state needed to resume the agentic loop after the user
    resolves the approval via the toolbar interaction.
    """

    def __init__(self, interaction, tool_id, function_name, tool, arguments,
                 context, remaining_tool_calls=None):
        super().__init__("Tool approval suspended — yield to main prompt")
        # ToolApprovalPending interaction (result readable after resolve)
        self.interaction = interaction
        # Tool call identity
        self.tool_id = tool_id
        self.function_name = function_name
        # Execution state for resume
        self.tool = tool
        self.arguments = arguments
        self.context = context
        # Remaining tool calls in the batch (for sequential execution)
        self.remaining_tool_calls = remaining_tool_calls or []


class SelectionSuspended(Exception):
    """Raised when select_option suspends to the main prompt for user input.

    Carries all state needed to resume the agentic loop after the user
    makes a selection via the toolbar interaction.
    """

    def __init__(self, interaction, tool_id, function_name, tool, arguments,
                 context, questions, remaining_tool_calls=None):
        super().__init__("Select_option suspended — yield to main prompt")
        self.interaction = interaction
        self.tool_id = tool_id
        self.function_name = function_name
        self.tool = tool
        self.arguments = arguments
        self.context = context
        # The validated questions list (with custom input option appended)
        self.questions = questions
        self.remaining_tool_calls = remaining_tool_calls or []


class BoundaryApprovalSuspended(Exception):
    """Raised when a tool hits a path-boundary error and yields to the main prompt.

    Carries all state needed to resume the agentic loop after the user
    grants or denies full filesystem access. On resume, if access is
    granted, the original tool is re-executed with the boundary lifted.
    """

    def __init__(self, interaction, tool_id, function_name, tool, arguments,
                 context, path_arg, access_reason, remaining_tool_calls=None):
        super().__init__("Boundary approval suspended — yield to main prompt")
        self.interaction = interaction
        self.tool_id = tool_id
        self.function_name = function_name
        self.tool = tool
        self.arguments = arguments
        self.context = context
        self.path_arg = path_arg
        self.access_reason = access_reason
        self.remaining_tool_calls = remaining_tool_calls or []

from core.tool_feedback import (
    vault_root_str,
    _print_or_append,
    strip_leading_task_list_echo,
    build_read_file_label,
    build_tool_label,
    display_tool_feedback,
)
from ui.sub_agent_panel import SubAgentPanel
from tools.helpers.path_resolver import extract_boundary_path, is_boundary_error, set_full_filesystem_access


def _handle_empty_response(empty_response_count, console):
    """Handle empty response from model.

    Returns:
        tuple: (should_continue, updated_count)
    """
    empty_response_count += 1
    if empty_response_count >= 2:
        console.print("[red]Error: model returned empty response with no tool calls.[/red]")
        return False, empty_response_count
    return True, empty_response_count



def _handle_tool_limit_reached(chat_manager, console):
    """Handle case when tool call limit is exceeded.

    Returns:
        bool: True if handled successfully, False if error
    """
    chat_manager.messages.append({
        "role": "user",
        "content": "Tool limit reached. Provide your answer without calling tools."
    })

    try:
        response = chat_manager.client.chat_completion(
            chat_manager.messages, stream=False, tools=None
        )
    except LLMError as e:
        console.print(f"[red]LLM Error: {e}[/red]")
        return False

    if isinstance(response, dict) and 'usage' in response:
        provider_cfg = get_provider_config(chat_manager.client.provider)
        chat_manager.token_tracker.add_usage(
            response,
            model_name=provider_cfg.get("model", ""),
        )
    try:
        final_message = response["choices"][0]["message"]
    except (KeyError, IndexError):
        console.print("[red]Error: invalid response from model[/red]")
        return False

    content = final_message.get("content", "").strip()
    if content:
        md = Markdown(left_align_headings(content), code_theme=MonokaiDarkBGStyle, justify="left")
        console.print(md)
        chat_manager.messages.append(final_message)
        console.print()
        return True

    console.print("[red]Error: model returned empty response after tool limit reached.[/red]")
    return False

class AgenticOrchestrator:
    """Orchestrates the agentic tool-calling loop.

    This class encapsulates the complex logic of coordinating LLM interactions
    with tool calling, providing a cleaner, more maintainable structure.
    """

    def __init__(self, chat_manager, repo_root, rg_exe_path, console, debug_mode, suppress_result_display=False, is_sub_agent=False, panel_updater=None, force_parallel_execution=False, cron_job_id=None, cron_allowlist=None, cron_interactive=False, edit_approval_handler=None, create_file_handler=None, command_approval_handler=None, command_approval_awaiter=None, command_send_approval_fn=None, approval_timeout=300):
        """Initialize the orchestrator.

        Args:
            chat_manager: ChatManager instance for state management
            repo_root: Path to repository root
            rg_exe_path: Path to rg.exe
            console: Rich console for output
            debug_mode: Whether to show debug output
            suppress_result_display: If True, suppress final LLM response display (for research agent)
            is_sub_agent: If True, running as sub-agent (for visual framing)
            panel_updater: Optional SubAgentPanel callback for live panel updates
            force_parallel_execution: If True, force parallel execution (for sub-agent)
            cron_job_id: Optional cron job ID for command allow list gating
            cron_allowlist: Optional CronAllowlist instance for cron command gating
            cron_interactive: If True, cron job is running in interactive test mode
            edit_approval_handler: Optional override for edit approval (default: handle_edit_approval)
            create_file_handler: Optional override for create_file execution.
            command_approval_handler: Optional override for command approval; only set by swarm workers (default: None → uses inline suspend path)
            command_approval_awaiter: Optional ApprovalAwaiter for worker command approvals
            command_send_approval_fn: Optional callable to send approval_request to server
            approval_timeout: Timeout for command approval in seconds
        """
        self.chat_manager = chat_manager
        self.repo_root = repo_root
        self.rg_exe_path = rg_exe_path
        self.console = console
        self.debug_mode = debug_mode
        self.suppress_result_display = suppress_result_display
        self.is_sub_agent = is_sub_agent
        self.panel_updater = panel_updater
        self.force_parallel_execution = force_parallel_execution
        self.cron_job_id = cron_job_id
        self.cron_allowlist = cron_allowlist
        self.cron_interactive = cron_interactive
        self.edit_approval_handler = edit_approval_handler
        self.create_file_handler = create_file_handler
        self.command_approval_handler = command_approval_handler
        self.command_approval_awaiter = command_approval_awaiter
        self.command_send_approval_fn = command_send_approval_fn
        self.approval_timeout = approval_timeout
        self.tool_calls_count = 0
        self.empty_response_count = 0
        self.gitignore_spec = chat_manager.get_gitignore_spec(repo_root)
        # Snapshot the cancel event for this turn.  The chat_manager's
        # event can be cleared when a new turn starts (Ctrl+C → new
        # message), but this orchestrator keeps its reference to the
        # already-set event so it still sees the cancel signal.
        self._cancel_event = chat_manager.get_agent_cancel_event()
        # For parallel execution: temporary console override
        self._parallel_context = {}
        # Suspension state for tool approval/selection handoff to main prompt
        self._suspended_exc: Optional[ToolApprovalSuspended | SelectionSuspended | BoundaryApprovalSuspended] = None
        # Initialize vault session with known repo_root (for project folder derivation)
        try:
            from tools.obsidian import init_session
            init_session(repo_root)
        except Exception as e:
            logger.warning("Failed to initialize vault session: %s", e)
        # Bootstrap memory system (creates ~/.bone/ and .bone/ dirs + files if missing)
        try:
            from core.memory import MemoryManager
            MemoryManager.get_instance(repo_root).ensure_exists()
        except Exception as e:
            logger.warning("Failed to initialize memory system: %s", e)


    def _cancel_requested(self) -> bool:
        """Check if this turn's cancel event has been set.

        Uses the event snapshot taken at construction time so that
        ``clear_agent_cancel()`` on chat_manager (for a new turn) does
        not revive a cancelled turn.
        """
        return self._cancel_event.is_set()

    def set_cancel_event(self, event):
        """Replace the orchestrator's cancel event.

        External callers use this to wire the orchestrator into a parent
        cancel signal (e.g., ``sub_agent`` connects inner orchestrators
        to the parent's cancel event) or to refresh the event reference
        after ``chat_manager.clear_agent_cancel()`` allocates a new event
        for the next turn (swarm workers).  Accepting the event directly
        avoids coupling call sites to the private ``_cancel_event``
        attribute.
        """
        self._cancel_event = event

    def _get_console(self):
        """Get the console for output, respecting parallel execution context.

        Returns:
            Console object or None if suppressed during parallel execution
        """
        # Check if we're in a parallel context with suppressed console
        return self._parallel_context.get('console', self.console)

    def _get_effective_tools(self, allowed_tools=None, allow_active_plugins=False):
        """Return tool schemas allowed for the current run."""
        from tools.helpers.base import ToolRegistry

        tools = TOOLS()
        if allowed_tools is None:
            if getattr(self.chat_manager, "swarm_admin_mode", False):
                admin_blocked_tools = {"execute_command"}
                return [
                    tool for tool in tools
                    if tool.get("function", {}).get("name") not in admin_blocked_tools
                ]
            blocked = ADMIN_SWARM_TOOL_NAMES
            return [
                tool for tool in tools
                if tool.get("function", {}).get("name") not in blocked
            ]

        effective_names = set(allowed_tools)
        if allow_active_plugins:
            effective_names.update(ToolRegistry.active_plugin_names())

        if not effective_names:
            return []

        return [tool for tool in tools if tool["function"]["name"] in effective_names]

    def run(self, user_input: str | list[dict[str, Any]], thinking_indicator=None, allowed_tools=None, allow_active_plugins=False):
        """Main orchestration loop.

        Args:
            user_input: User's input message content
            thinking_indicator: Optional ThinkingIndicator instance
            allowed_tools: Optional list of allowed tool names (for research)
            allow_active_plugins: Whether to include active plugin tools in restricted runs

        Returns:
            "done" on normal completion, "suspended" if a tool approval was
            handed off to the main prompt and the caller must resume.
        """
        self._current_allowed_tools = allowed_tools
        self._current_allow_active_plugins = allow_active_plugins

        # Clear completed task list on each new user turn.
        task_list = getattr(self.chat_manager, "task_list", None)
        if task_list:
            task_list.clear()
            self.chat_manager.task_list_title = None

        user_message = {"role": "user", "content": sanitize_message_content(user_input)}

        # Append user message
        self.chat_manager.messages.append(user_message)

        # Log user message
        self.chat_manager.log_message(user_message)

        from tools.helpers.base import ToolRegistry

        try:
            while True:
                if self._cancel_requested():
                    raise AgentTurnCancelled()

                # Decrement plugin TTLs after previous iteration's tool execution.
                # Evicted plugins are excluded from the next LLM call's context window.
                evicted = ToolRegistry.decrement_plugin_ttls()
                if evicted and self.debug_mode:
                    self.console.print(f"[dim]Plugins evicted (TTL expired): {evicted}[/dim]")

                # ── Mid-turn swarm inbox injection ──────────────────────
                self._drain_and_inject_swarm_messages()

                # Get response from LLM
                response = self._get_llm_response(
                    allowed_tools=allowed_tools,
                    allow_active_plugins=allow_active_plugins,
                )
                if response is None:
                    return
                if self._cancel_requested():
                    raise AgentTurnCancelled()

                # Auto-compact if over token threshold (applies to both main agent and subagent)
                self.chat_manager.maybe_auto_compact()

                # Check for tool calls
                tool_calls = response.get("tool_calls")

                if not tool_calls:
                    if self._handle_final_response(response, thinking_indicator):
                        return
                else:
                    should_exit = self._handle_tool_calls(
                        response,
                        thinking_indicator,
                        allowed_tools,
                        allow_active_plugins=allow_active_plugins,
                    )
                    if should_exit:
                        return
        except (ToolApprovalSuspended, SelectionSuspended, BoundaryApprovalSuspended) as e:
            # Save state for later resume; return to caller so the main
            # prompt loop can present the interaction.
            self._suspended_exc = e
            return "suspended"
        except AgentTurnCancelled:
            msgs = self.chat_manager.messages
            if msgs and msgs[-1] is user_message:
                msgs.pop()
                self.chat_manager.sync_log()
            return "cancelled"
        except KeyboardInterrupt:
            msgs = self.chat_manager.messages
            if msgs and msgs[-1] is user_message:
                msgs.pop()
                self.chat_manager.sync_log()
            raise

    def _get_llm_response(self, allowed_tools=None, allow_active_plugins=False):
        """Get next LLM response with tool definitions.

        Includes automatic retry with live countdown for timeout/connection errors.
        Retries up to 3 times with a 5-second countdown between attempts.

        Args:
            allowed_tools: Optional list of allowed tool names (overrides mode-based filtering)
            allow_active_plugins: Whether to include active plugin tools in restricted runs

        Returns:
            Response dict from LLM, or None if error occurred
        """
        if self._cancel_requested():
            raise AgentTurnCancelled()

        # Pre-send guard: ensure context fits before the LLM call
        self.chat_manager.ensure_context_fits(console=self.console)

        # Use allowed_tools if provided, otherwise use mode-based filtering
        if allowed_tools is not None and not allowed_tools and not allow_active_plugins:
            self.console.print("[red]Error: allowed_tools is empty[/red]")
            return None

        tools = self._get_effective_tools(
            allowed_tools=allowed_tools,
            allow_active_plugins=allow_active_plugins,
        )
        if allowed_tools is not None and self.debug_mode:
            tool_names = [t["function"]["name"] for t in tools]
            self.console.print(f"[dim]Available tools: {tool_names}[/dim]")

        # Retry loop for timeout/connection errors
        last_error = None
        for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
            try:
                response = self.chat_manager.client.chat_completion(
                    self.chat_manager.messages, stream=False, tools=tools
                )
                if self._cancel_requested():
                    raise AgentTurnCancelled()
            except LLMError as e:
                last_error = e

                # Check if this error is retryable
                if is_retryable_error(e) and attempt < RETRY_MAX_ATTEMPTS:
                    delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                    wait_ok = wait_with_cancel_message(self.console, delay)
                    if not wait_ok:
                        return None
                    continue
                else:
                    # Non-retryable error or final attempt exhausted
                    detail_lines = []
                    details = getattr(e, "details", {})
                    for key, value in details.items():
                        value_str = str(value)
                        if "\n" in value_str and key != "original_error":
                            detail_lines.append(f"{key}: {value_str}")
                    detailed_error = str(e)
                    if detail_lines:
                        detailed_error += "\n\n" + "\n\n".join(detail_lines)

                    if self.is_sub_agent:
                        raise LLMError(detailed_error, details=getattr(e, "details", {}))

                    self.console.print(f"[red]LLM Error: {e}[/red]")
                    if detail_lines:
                        self.console.print(f"[dim]{detail_lines[0]}[/dim]", markup=False)
                    return None

            # Successful response — parse and return
            # Extract and track usage data
            if isinstance(response, dict) and 'usage' in response:
                provider_cfg = get_provider_config(self.chat_manager.client.provider)
                self.chat_manager.token_tracker.add_usage(
                    response,
                    model_name=provider_cfg.get("model", ""),
                )

            try:
                message = response["choices"][0]["message"]
            except (KeyError, IndexError):
                self.console.print("[red]Error: invalid response from model[/red]")
                return None

            return message

        # Should not reach here, but handle gracefully
        self.console.print(f"[red]LLM Error: {last_error}[/red]")
        return None

    def _handle_final_response(self, response, thinking_indicator=None):
        """Handle non-tool-call response (final answer).

        Args:
            response: Message dict from LLM
            thinking_indicator: Optional ThinkingIndicator instance to clear before displaying

        Returns:
            True if handled successfully, False if should continue looping
        """
        content = response.get("content", "")
        content = strip_leading_task_list_echo(
            content,
            getattr(self.chat_manager, "task_list", None) or [],
            getattr(self.chat_manager, "task_list_title", None),
        )
        # Strip leading "Assistant: " prefix that some models may output
        if content.startswith("Assistant: "):
            content = content[len("Assistant: "):]
        content = content.lstrip()
        if content and content.strip():
            # Clear thinking indicator before printing response to avoid flash
            if thinking_indicator:
                thinking_indicator.stop(reset=True)
            # Only display to user if result display is not suppressed
            if not self.suppress_result_display:
                md = Markdown(left_align_headings(content), code_theme=MonokaiDarkBGStyle, justify="left")
                self.console.print(md)
            # Always append to message history (AI needs the result regardless)
            response = dict(response)
            response["content"] = content
            self.chat_manager.messages.append(response)
            # Log assistant response
            self.chat_manager.log_message(response)

            # NEW: Compact tool results after final answer (per-message compaction)
            self.chat_manager.compact_tool_results(skip_token_update=True)

            # Update context tokens with current run's effective tools
            tools_for_mode = self._get_effective_tools(
                allowed_tools=getattr(self, "_current_allowed_tools", None),
                allow_active_plugins=getattr(self, "_current_allow_active_plugins", False),
            )
            self.chat_manager._update_context_tokens(tools_for_mode)

            self.console.print()
            return True

        # Empty response with no tools
        should_continue, self.empty_response_count = _handle_empty_response(
            self.empty_response_count, self.console
        )
        return not should_continue

    def _handle_tool_calls(self, response, thinking_indicator, allowed_tools=None, allow_active_plugins=False):
        """Process tool calls and display accompanying content.

        Args:
            response: Full message dict from LLM (includes content and tool_calls)
            thinking_indicator: Optional ThinkingIndicator instance
            allowed_tools: Optional list of allowed tool names
            allow_active_plugins: Whether to allow active plugin tools in restricted runs

        Returns:
            True if should exit the orchestration loop
        """
        # Extract tool_calls from response
        tool_calls = response.get("tool_calls")
        if not tool_calls:
            return False  # Should not happen if called correctly

        # Append assistant message with ALL tool calls (include content if present)
        # This must happen BEFORE filtering so the LLM sees its original intent.
        # Keep provider-specific fields too; DeepSeek reasoning models require
        # reasoning_content to be passed back on later requests.
        content = (response.get("content") or "").strip()
        assistant_msg = dict(response)
        assistant_msg["role"] = "assistant"
        assistant_msg["tool_calls"] = tool_calls
        if content:
            assistant_msg["content"] = content
        elif "content" in assistant_msg:
            assistant_msg.pop("content")

        # Record message count before assistant message for potential
        # rollback if the subagent is cancelled mid-flight.
        _msg_count_before = len(self.chat_manager.messages)
        self.chat_manager.messages.append(assistant_msg)
        # Log assistant tool call message
        self.chat_manager.log_message(assistant_msg)

        # NEW: Filter out non-allowed tools BEFORE execution
        # This silently removes unknown tools or tools not in the allowed whitelist
        # to prevent error messages from reaching the user while allowing the agent
        # to continue with alternative tools.
        from tools.helpers.base import ToolRegistry

        filtered_calls = []
        filtered_tool_ids = []  # Track filtered tool IDs to provide feedback

        for tool_call in tool_calls:
            function_name = tool_call.get("function", {}).get("name")

            # Check if tool exists in registry
            if not ToolRegistry.get(function_name):
                # Silent fail - skip this tool call entirely
                # Agent will receive empty result and can retry with correct tool
                if self.debug_mode:
                    self.console.print(f"[dim]Silently filtered unknown tool: {function_name}[/dim]")
                filtered_tool_ids.append(tool_call.get("id"))
                continue

            # Check if tool is in the effective allowlist for this run.
            effective_allowed_tools = None
            if allowed_tools is not None:
                effective_allowed_tools = set(allowed_tools)
                if allow_active_plugins:
                    effective_allowed_tools.update(ToolRegistry.active_plugin_names())

            if effective_allowed_tools is not None and function_name not in effective_allowed_tools:
                # Silent fail - skip this tool
                if self.debug_mode:
                    self.console.print(f"[dim]Silently filtered non-allowed tool: {function_name}[/dim]")
                filtered_tool_ids.append(tool_call.get("id"))
                continue

            _admin_swarm_tools = ADMIN_SWARM_TOOL_NAMES
            if function_name in _admin_swarm_tools and not getattr(self.chat_manager, "swarm_admin_mode", False):
                # Swarm tools are only available to the admin agent.
                if self.debug_mode:
                    self.console.print(f"[dim]Silently filtered non-admin swarm tool: {function_name}[/dim]")
                filtered_tool_ids.append(tool_call.get("id"))
                continue

            filtered_calls.append(tool_call)

        # Replace with filtered list
        tool_calls = filtered_calls

        # Provide feedback to agent for filtered tools
        # This allows the agent to understand which tools were not available
        # without showing error messages to the user
        if filtered_tool_ids:
            for tool_id in filtered_tool_ids:
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": "exit_code=1\nTool not available. Please use the available tools from the function list."
                }
                self.chat_manager.messages.append(tool_msg)
                self.chat_manager.log_message(tool_msg)

        # If all tools were filtered, return early
        if not tool_calls:
            if self.debug_mode:
                self.console.print("[dim]All tool calls were filtered, continuing...[/dim]")
            return False

        self.empty_response_count = 0
        self.tool_calls_count += 1

        if self.tool_calls_count > MAX_TOOL_CALLS:
            return not _handle_tool_limit_reached(self.chat_manager, self.console)

        # Display conversational content if present
        # Skip if calling sub_agent OR if we ARE a sub-agent (sub-agent panel provides context)
        is_calling_sub_agent = any(
            tool.get("function", {}).get("name") == "sub_agent"
            for tool in tool_calls
        )
        # Route to panel if we're a sub-agent with a panel_updater, otherwise print to console
        if content:
            if self.is_sub_agent and self.panel_updater:
                # Sub-agent: send thinking to panel instead of console
                self.panel_updater.append(content)
            elif not is_calling_sub_agent:
                # Main agent: print to console (unless calling sub_agent)
                md = Markdown(left_align_headings(content), code_theme=MonokaiDarkBGStyle, justify="left")
                self.console.print(md)
                self.console.print()

        # Check if we should use parallel execution
        use_parallel = (
            tool_settings.enable_parallel_execution and
            len(tool_calls) > 1
        )

        # Force sequential if any edit_file or execute_command in the batch (safety)
        if use_parallel:
            for tool_call in tool_calls:
                tool_name = tool_call.get("function", {}).get("name")
                if tool_name == "edit_file":
                    use_parallel = False
                    if self.debug_mode:
                        self.console.print("[dim]Forcing sequential execution (edit_file detected)[/dim]")
                    break
                elif tool_name == "execute_command":
                    use_parallel = False
                    if self.debug_mode:
                        self.console.print("[dim]Forcing sequential execution (execute_command detected)[/dim]")
                    break
                elif tool_name == "sub_agent":
                    use_parallel = False
                    if self.debug_mode:
                        self.console.print("[dim]Forcing sequential execution (sub_agent detected)[/dim]")
                    break
                elif tool_name == "select_option":
                    use_parallel = False
                    if self.debug_mode:
                        self.console.print("[dim]Forcing sequential execution (select_option detected)[/dim]")
                    break

        if use_parallel and self.debug_mode:
            self.console.print(f"[#5F9EA0]Executing {len(tool_calls)} tools in parallel[/#5F9EA0]")

        # Lock compaction during tool execution to prevent orphaning tool_call_ids
        self.chat_manager.set_compaction_lock(True)

        try:
            if use_parallel:
                result = self._execute_tools_parallel(tool_calls, thinking_indicator)
            else:
                result = self._execute_tools_sequential(tool_calls, thinking_indicator)
        finally:
            # Always unlock — even on suspension the resume path will
            # handle compaction at its own pace.
            self.chat_manager.set_compaction_lock(False)

        # If the subagent was cancelled, roll back the assistant message
        # — we didn't append any tool result, so keeping it would leave
        # a stale tool_call that the LLM would never see fulfilled.
        if result and is_calling_sub_agent:
            if len(self.chat_manager.messages) > _msg_count_before:
                del self.chat_manager.messages[_msg_count_before:]
                self.chat_manager.sync_log()

        return result

    def _execute_tools_sequential(self, tool_calls, thinking_indicator):
        """Execute tools one at a time (original behavior).

        Args:
            tool_calls: List of tool call dicts from LLM
            thinking_indicator: Optional ThinkingIndicator instance

        Returns:
            True if should exit the orchestration loop

        Raises:
            ToolApprovalSuspended: If a tool approval was handed off to the
                main prompt.  The exception carries ``remaining_tool_calls``
                so the resume path can finish the batch.
        """
        end_loop = False

        for i, tool_call in enumerate(tool_calls):
            # Check for Ctrl+C cancellation between tool executions.
            # Without this, a multi-tool batch blocks cancellation until
            # every tool finishes — which can be seconds or minutes.
            if self._cancel_requested():
                raise AgentTurnCancelled()

            tool_id = tool_call["id"]
            try:
                should_exit, tool_result = self._process_single_tool_call(
                    tool_call, thinking_indicator
                )
            except (ToolApprovalSuspended, SelectionSuspended, BoundaryApprovalSuspended) as e:
                # Attach remaining tool calls (those after this one) so the
                # resume path can process them after the interaction is resolved.
                e.remaining_tool_calls = tool_calls[i + 1:]
                # Re-raise so _handle_tool_calls → run() can catch it.
                raise

            if should_exit:
                # Cancel was selected - append this result and break immediately
                if tool_result is not None and tool_result is not False:
                    if isinstance(tool_result, Text):
                        content_for_agent = f"exit_code=0\n{str(tool_result)}"
                    else:
                        content_for_agent = str(tool_result)
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": content_for_agent
                    }
                    self.chat_manager.messages.append(tool_msg)
                    self.chat_manager.log_message(tool_msg)
                return True  # Exit orchestration loop immediately

            # Append tool result if not skipped (guidance mode)
            if tool_result is not None and tool_result is not False:
                # Add exit_code prefix for agent consumption
                if isinstance(tool_result, Text):
                    # Rich Text object = successful edit (exit_code=0)
                    content_for_agent = f"exit_code=0\n{str(tool_result)}"
                else:
                    content_for_agent = str(tool_result)
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": content_for_agent
                }
                self.chat_manager.messages.append(tool_msg)
                # Log tool result
                self.chat_manager.log_message(tool_msg)

        # Compact completed tool blocks once after all tools complete
        self.chat_manager.compact_tool_results(skip_token_update=True)

        # Update context tokens with current run's effective tools
        tools_for_mode = self._get_effective_tools(
            allowed_tools=getattr(self, "_current_allowed_tools", None),
            allow_active_plugins=getattr(self, "_current_allow_active_plugins", False),
        )
        self.chat_manager._update_context_tokens(tools_for_mode)

        # Pre-send guard: ensure context fits before next LLM call
        self.chat_manager.ensure_context_fits(console=self.console)

        return end_loop

    def _execute_tools_parallel(self, tool_calls, thinking_indicator):
        """Execute multiple tools concurrently.

        Args:
            tool_calls: List of tool call dicts from LLM (already filtered)
            thinking_indicator: Optional ThinkingIndicator instance

        Returns:
            True if should exit the orchestration loop
        """
        if not tool_calls:
            return False
        from tools.helpers.parallel_executor import ParallelToolExecutor, ToolCall

        # Suppress console output in handlers during parallel execution
        # We'll display results ourselves in order below
        self._parallel_context['console'] = None

        try:
            # Prepare context
            context = {
                'thinking_indicator': thinking_indicator,
                'repo_root': self.repo_root,
                'chat_manager': self.chat_manager,
                'rg_exe_path': self.rg_exe_path,
                'debug_mode': self.debug_mode,
                'gitignore_spec': self.gitignore_spec,
                'panel_updater': self.panel_updater,
                'vault_root': vault_root_str(),
                'create_file_handler': self.create_file_handler,
            }

            # Convert to ToolCall objects
            tool_call_objs = []
            for i, tc in enumerate(tool_calls):
                try:
                    arguments = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    # Invalid JSON - handle inline for this tool
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": "exit_code=1\nInvalid JSON arguments"
                    }
                    self.chat_manager.messages.append(tool_msg)
                    self.chat_manager.log_message(tool_msg)
                    continue

                tool_call_objs.append(
                    ToolCall(
                        tool_id=tc["id"],
                        function_name=tc["function"]["name"],
                        arguments=arguments,
                        call_index=i
                    )
                )

            if not tool_call_objs:
                # All tools had invalid arguments
                return False

            # Create executor
            executor = ParallelToolExecutor(
                max_workers=tool_settings.max_parallel_workers
            )

            # Execute in parallel
            results, _ = executor.execute_tools(
                tool_call_objs,
                context
            )

            # Display results with labels (staggered: label → feedback, like sequential mode)
            for result in results:
                if result.success:
                    # Get tool call info
                    tool_call = tool_calls[result.call_index]
                    function_name = tool_call.get("function", {}).get("name", "")
                    arguments = tool_call.get("function", {}).get("arguments", "{}")
                    args_dict = json.loads(arguments) if isinstance(arguments, str) else arguments

                    # Label builders
                    label_builders = {
                        "rg": lambda a: f"rg: {a.get('pattern', '')[:40]}",
                        "read_file": lambda a: build_read_file_label(
                            a.get('path_str', ''),
                            a.get('start_line'),
                            a.get('max_lines'),
                            with_colon=True
                        ),
                        "list_directory": lambda a: f"list_directory: {a.get('path_str', '')}",
                        "search_plugins": lambda a: f"search_plugins: {a.get('query', '')}",
                        "create_file": lambda a: f"create_file: {a.get('path_str', '')}",
                        "web_search": lambda a: f"web search | {a.get('query', '')}",
                        "create_task_list": lambda a: "create_task_list",
                        "complete_task": lambda a: "complete_task",
                        "show_task_list": lambda a: "show_task_list",
                    }

                    # Print the label first (staggered output: label → feedback)
                    label_builder = label_builders.get(function_name, lambda a: function_name)
                    try:
                        label = label_builder(args_dict)
                        
                        # Print the label before feedback (matches sequential path)
                        if not self.panel_updater and function_name not in ("create_task_list", "complete_task", "show_task_list"):
                            label_text = f"[grey]{label}[/grey]" if not function_name.startswith("web search") else f"[bold #5F9EA0]{label}[/bold #5F9EA0]"
                            self.console.print(label_text, highlight=False)
                            self.console.file.flush()
                        
                        # For task list tools: only show the task list, no label duplication
                        # Skip the feedback display below since we already showed it
                        continue_flag = False
                        if function_name in ("create_task_list", "complete_task", "show_task_list"):
                            exit_code = extract_exit_code(result.result)
                            if exit_code == 0 or exit_code is None:
                                rendered = result.result
                                if rendered.startswith("exit_code="):
                                    rendered = "\n".join(rendered.splitlines()[1:])
                                if self.panel_updater:
                                    self.panel_updater.append(rendered.strip())
                                else:
                                    self.console.print(rendered.strip(), markup=True)
                                    self.console.print()
                            else:
                                first_two = "\n".join(result.result.splitlines()[:2]).strip()
                                if self.panel_updater:
                                    self.panel_updater.append(first_two or result.result.strip())
                                else:
                                    self.console.print(first_two or result.result.strip(), markup=False)
                                    self.console.print()
                            continue_flag = True
                            label = function_name
                    except Exception:
                        label_text = f"[grey]{function_name}[/grey]"
                        if not self.panel_updater:
                            self.console.print(label_text, highlight=False)
                            self.console.file.flush()
                        label = function_name  # Fallback for error path
                        continue_flag = False

                    # Display feedback immediately after label (no buffering)
                    # Skip for task list tools since they handled their own display
                    if continue_flag:
                        continue
                    try:
                        if function_name == "edit_file" and result.requires_approval:
                            # Handle approval workflow for edit_file in parallel mode
                            thinking_indicator = context.get('thinking_indicator')
                            if self.edit_approval_handler:
                                approved_result, should_exit = self.edit_approval_handler(args_dict, self.repo_root, self.console, self.gitignore_spec, vault_root_str)
                                result.result = approved_result
                                if should_exit:
                                    result.should_exit = True
                            else:
                                preview, is_valid = resolve_edit_preview(result.result)
                                if is_valid:
                                    approved_result, should_exit = handle_edit_approval(
                                        preview, args_dict.get('path', ''), args_dict,
                                        self.console, thinking_indicator,
                                        self.chat_manager.approve_mode,
                                        lambda mode=None: self.chat_manager.cycle_approve_mode(mode),
                                        self.repo_root, self.gitignore_spec,
                                        vault_root_str, self.chat_manager)
                                    result.result = approved_result
                                    if should_exit:
                                        result.should_exit = True
                        elif label:
                            display_tool_feedback(label, result.result, self.console, panel_updater=self.panel_updater)
                            # Force flush to ensure immediate output
                            if not self.panel_updater:
                                self.console.file.flush()
                        else:
                            completion_text = f"[dim]{function_name} completed[/dim]"
                            if self.panel_updater:
                                self.panel_updater.append(completion_text)
                            else:
                                self.console.print(completion_text, highlight=False)
                                self.console.file.flush()
                    except Exception:
                        completion_text = f"[dim]{function_name} completed[/dim]"
                        if self.panel_updater:
                            self.panel_updater.append(completion_text)
                        else:
                            self.console.print(completion_text, highlight=False)
                            self.console.file.flush()
                else:
                    error_msg = result.error or result.result
                    error_text = f"[red]{error_msg}[/red]"
                    if self.panel_updater:
                        self.panel_updater.append(error_text)
                    else:
                        self.console.print(error_text)
                        self.console.file.flush()

            # Display summary
            success_count = sum(1 for r in results if r.success)
            if self.debug_mode:
                self.console.print(
                    f"[dim]Parallel execution: {success_count}/{len(results)} succeeded[/dim]"
                )

            # Append all results to chat history
            end_loop = False
            for result in results:
                if result.success:
                    # Check if tool requested exit
                    if result.should_exit:
                        end_loop = True

                    # Add exit_code prefix for agent consumption (Rich Text = success)
                    if isinstance(result.result, Text):
                        content_for_agent = f"exit_code=0\n{str(result.result)}"
                    else:
                        content_for_agent = str(result.result)
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": result.tool_id,
                        "content": content_for_agent
                    }
                    self.chat_manager.messages.append(tool_msg)
                    # Log tool result
                    self.chat_manager.log_message(tool_msg)

                    self._check_swarm_completion(
                        tool_calls[result.call_index].get("function", {}).get("name", "")
                    )
                else:
                    # Tool failed
                    error_msg = result.error or result.result
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": result.tool_id,
                        "content": f"exit_code=1\n{error_msg}"
                    }
                    self.chat_manager.messages.append(tool_msg)
                    # Log tool result
                    self.chat_manager.log_message(tool_msg)

            # Mid-loop compaction: compact older completed tool blocks
            # after all parallel results are appended (safe — only compacts completed blocks)
            self.chat_manager.compact_tool_results(skip_token_update=True)

            # Update context tokens with current run's effective tools
            tools_for_mode = self._get_effective_tools(
                allowed_tools=getattr(self, "_current_allowed_tools", None),
                allow_active_plugins=getattr(self, "_current_allow_active_plugins", False),
            )
            self.chat_manager._update_context_tokens(tools_for_mode)

            # Pre-send guard: ensure context fits before next LLM call
            self.chat_manager.ensure_context_fits(console=self.console)

            return end_loop
        finally:
            # Restore console output
            self._parallel_context['console'] = self.console

    def _boundary_prompt(self, path_str, access_reason, thinking_indicator=None,
                         tool_id=None, function_name=None, tool=None,
                         arguments=None, context=None):
        """Prompt the user to grant filesystem access for a path outside boundaries.

        Called after a tool returns a boundary error. In normal main-agent
        runtime, suspends to the main prompt toolbar (via
        ``BoundaryApprovalSuspended``). In sub-agent / cron / no-chat-manager
        contexts, falls back to the blocking ``ToolConfirmationPanel.run()``.

        Args:
            path_str: The path that triggered the boundary violation.
            access_reason: Agent-provided reason for requesting broader access.
            thinking_indicator: Optional ThinkingIndicator instance to pause.
            tool_id: Tool call ID (required for suspension path).
            function_name: Tool function name (required for suspension path).
            tool: The tool instance (required for suspension path).
            arguments: Tool arguments dict (required for suspension path).
            context: Tool execution context dict (required for suspension path).

        Returns:
            True if user granted access, False if denied (fallback path only).
            The suspension path raises ``BoundaryApprovalSuspended`` instead.
        """
        console = self._get_console()
        if console is None:
            return False

        access_reason = (access_reason or "").strip()
        if not access_reason:
            return False

        # ---- Suspension path: yield to main prompt toolbar -----------------
        if self._can_suspend_approval():
            if thinking_indicator:
                thinking_indicator.stop()
            from ui.toolbar_interactions import (
                ToolApprovalPending,
                set_active_interaction,
                BOUNDARY_RESOLVED_SENTINEL,
            )
            prompt_display = path_str
            if len(prompt_display) > 60:
                prompt_display = prompt_display[:57] + "..."
            interaction = ToolApprovalPending(
                tool_command=f"Grant access to: {prompt_display}",
                reason=access_reason,
                is_edit_tool=False,
                exit_sentinel=BOUNDARY_RESOLVED_SENTINEL,
            )
            set_active_interaction(self.chat_manager, interaction)
            raise BoundaryApprovalSuspended(
                interaction=interaction,
                tool_id=tool_id,
                function_name=function_name,
                tool=tool,
                arguments=arguments,
                context=context,
                path_arg=path_str,
                access_reason=access_reason,
            )
            # Unreachable — exception always raised in this branch

        # ---- Fallback path: blocking panel for sub-agents / cron ----------
        prompt_reason = (
            f"Agent requested access outside project boundary: {path_str}\n\n"
            f"Requested purpose: {access_reason}"
        )

        from ui.tool_confirmation import ToolConfirmationPanel
        panel = ToolConfirmationPanel(
            'Grant filesystem access',
            reason=prompt_reason,
            is_edit_tool=False,
            chat_manager=self.chat_manager,
        )
        suspended_panel = False
        if self.is_sub_agent and self.panel_updater and hasattr(self.panel_updater, "suspend"):
            suspended_panel = self.panel_updater.suspend()
        if thinking_indicator:
            thinking_indicator.stop()
        try:
            action, _ = panel.run()
        finally:
            if thinking_indicator:
                thinking_indicator.start()
            if suspended_panel and hasattr(self.panel_updater, "resume"):
                self.panel_updater.resume()

        if action == "accept":
            console.print("[yellow]Full filesystem access granted[/yellow]\n")
            return True
        return False

    def _can_suspend_approval(self) -> bool:
        """Return True if tool approvals should suspend to the main prompt.

        Suspension is only valid when the main prompt loop is available
        to render and resolve the toolbar interaction.  Sub-agents, cron
        jobs, and custom-handler contexts use the blocking fallback.
        """
        return (
            not self.is_sub_agent
            and not self.cron_job_id
            and self.chat_manager is not None
        )

    def _process_single_tool_call(self, tool_call, thinking_indicator):
        """Process a single tool call.

        Args:
            tool_call: Tool call dict from LLM
            thinking_indicator: Optional ThinkingIndicator instance

        Returns:
            Tuple of (should_exit, tool_result)
            - should_exit: True if should exit orchestration loop
            - tool_result: Result string, or None if already appended, False if skipped
        """
        tool_id = tool_call["id"]
        function_name = tool_call["function"]["name"]

        # Parse arguments
        try:
            args_str = tool_call["function"]["arguments"]
            if args_str is None:
                return False, "Error: Tool arguments are missing."
            arguments = json.loads(args_str)
        except (json.JSONDecodeError, TypeError):
            return False, "Error: Invalid JSON arguments."

        # Create SubAgentPanel for sub_agent tool calls
        panel_to_use = self.panel_updater
        if function_name == "sub_agent":
            # Stop the thinking indicator before creating the panel —
            # SubAgentPanel takes over the toolbar and starts its own
            # refresh thread.  If ThinkingIndicator's background thread
            # is still running, it races with the panel for toolbar state
            # (spinner_message, spinner_active) causing garbled first-frame
            # output (e.g. "subage t:" truncation, duplicate "Thinking..."
            # line).  stop() joins the thread so the handoff is clean.
            if thinking_indicator:
                thinking_indicator.stop(reset=True)
            query = arguments.get("query", "")
            panel_to_use = SubAgentPanel(query, self.chat_manager)

        # Execute via tool registry
        from tools.helpers.base import ToolRegistry, build_context

        tool = ToolRegistry.get(function_name)
        if tool:
            # Reset TTL for plugin-tier tools when they are actually called
            if ToolRegistry.is_plugin_active(function_name):
                ToolRegistry.touch_plugin(function_name)
            try:
                context = build_context(
                    repo_root=self.repo_root,
                    console=self.console,
                    gitignore_spec=self.gitignore_spec,
                    debug_mode=self.debug_mode,
                    chat_manager=self.chat_manager,
                    rg_exe_path=self.rg_exe_path,
                    panel_updater=panel_to_use,
                    vault_root=vault_root_str()
                )
                # Determine terminal policy for thinking indicator management
                from tools.helpers.base import get_terminal_policy, TERMINAL_YIELD
                policy = get_terminal_policy(function_name)

                # Check if tool requires approval
                if tool.requires_approval:
                    # For edit_file: validate path then request approval
                    if function_name == "edit_file":
                        edit_path = arguments.get("path", "")
                        if not edit_path:
                            return False, "Error: path is required for edit_file."

                        # Normal edit: generate preview and request approval
                        result = tool.execute(arguments, context)

                        # Display preview
                        console = self._get_console()
                        if console:
                            preview, is_valid = resolve_edit_preview(result)
                            if is_valid:
                                if self.edit_approval_handler:
                                    approved_result, should_exit = self.edit_approval_handler(arguments, self.repo_root, console, self.gitignore_spec, vault_root_str)
                                    if should_exit:
                                        return True, approved_result
                                    result = approved_result
                                elif self._can_suspend_approval():
                                    # Auto-approve when accept_edits mode is active
                                    if self.chat_manager.approve_mode == "accept_edits":
                                        console.print(preview)
                                        console.print()
                                        from tools.edit import _execute_edit_file
                                        approved_result = _execute_edit_file(
                                            path=arguments.get('path'),
                                            search=arguments.get('search'),
                                            replace=arguments.get('replace'),
                                            repo_root=self.repo_root,
                                            console=console,
                                            gitignore_spec=self.gitignore_spec,
                                            context_lines=arguments.get('context_lines', 3),
                                            vault_root=vault_root_str()
                                        )
                                        if approved_result and isinstance(approved_result, str):
                                            result_lines = [line for line in approved_result.split('\n') if not line.startswith('exit_code=')]
                                            approved_result = '\n'.join(result_lines).strip()
                                        result = approved_result
                                    else:
                                        # Display preview then yield to main prompt
                                        console.print(preview)
                                        console.print()
                                        if thinking_indicator:
                                            thinking_indicator.stop()
                                        from ui.toolbar_interactions import (
                                            ToolApprovalPending,
                                            set_active_interaction,
                                        )
                                        interaction = ToolApprovalPending(
                                            tool_command=f"edit_file: {arguments.get('path', '')}",
                                            reason=arguments.get('reason', 'Apply file edit with above changes'),
                                            is_edit_tool=True,
                                            cycle_approve_mode=lambda mode=None: self.chat_manager.cycle_approve_mode(mode),
                                        )
                                        set_active_interaction(self.chat_manager, interaction)
                                        raise ToolApprovalSuspended(
                                            interaction=interaction,
                                            tool_id=tool_id,
                                            function_name=function_name,
                                            tool=tool,
                                            arguments=arguments,
                                            context=context,
                                        )
                                else:
                                    approved_result, should_exit = handle_edit_approval(
                                        preview, arguments.get('path', ''), arguments,
                                        console, thinking_indicator,
                                        self.chat_manager.approve_mode,
                                        lambda mode=None: self.chat_manager.cycle_approve_mode(mode),
                                        self.repo_root, self.gitignore_spec,
                                        vault_root_str, self.chat_manager)
                                    if should_exit:
                                        return True, approved_result
                                    result = approved_result
                        return False, str(result)
                    elif function_name == "execute_command":
                        console = self._get_console()
                        command = arguments.get('command', '')
                        if self.command_approval_handler:
                            result, should_exit, command_executed = self.command_approval_handler(
                                command=command,
                                arguments=arguments,
                                tool=tool,
                                context=context,
                                console=console,
                                debug_mode=self.debug_mode,
                                approve_awaiter=self.command_approval_awaiter,
                                send_approval_fn=self.command_send_approval_fn,
                                task_id=getattr(self, '_current_task_id', ''),
                                worker_id=getattr(self, '_current_worker_id', ''),
                                approval_timeout=self.approval_timeout,
                                cron_job_id=self.cron_job_id,
                                cron_allowlist=self.cron_allowlist,
                                cron_interactive=self.cron_interactive)
                            if should_exit:
                                return True, result
                            if command_executed:
                                label = build_tool_label(function_name, arguments)
                                label_text = f"[grey]{label}[/grey]"
                                if not self.panel_updater:
                                    console.print(label_text, highlight=False)
                                    console.file.flush()
                                display_tool_feedback(label, result, console, indent=self.is_sub_agent, panel_updater=self.panel_updater)
                            return False, result
                        elif self._can_suspend_approval():
                            # Check auto-approval conditions inline, then
                            # suspend to main prompt if interactive approval
                            # is actually needed.
                            from utils.safe_commands import is_git_command
                            from utils.validation import (
                                is_auto_approved_command,
                                check_for_silent_blocked_command,
                            )

                            # Silent block check
                            is_blocked, reprompt_msg = check_for_silent_blocked_command(command)
                            if is_blocked:
                                if self.debug_mode:
                                    console.print(f"[dim]Silently blocked command: {command.split()[0]}[/dim]")
                                return False, f"exit_code=1\n{reprompt_msg}"

                            # Auto-approve checks
                            auto_approve = is_auto_approved_command(command)
                            danger_auto = (
                                self.chat_manager.approve_mode == "danger"
                                and not is_git_command(command)
                            )
                            if auto_approve or danger_auto:
                                result = tool.execute(arguments, context)
                                label = build_tool_label(function_name, arguments)
                                label_text = f"[grey]{label}[/grey]"
                                if not self.panel_updater:
                                    console.print(label_text, highlight=False)
                                    console.file.flush()
                                display_tool_feedback(label, result, console, indent=self.is_sub_agent, panel_updater=self.panel_updater)
                                return False, result

                            # Needs interactive approval — suspend to main prompt
                            reason = arguments.get('reason', 'Execute shell command')
                            cmd_display = (
                                f"execute_command: {command[:80]}"
                                f"{'...' if len(command) > 80 else ''}"
                            )
                            if thinking_indicator:
                                thinking_indicator.stop()
                            from ui.toolbar_interactions import (
                                ToolApprovalPending,
                                set_active_interaction,
                            )
                            interaction = ToolApprovalPending(
                                tool_command=cmd_display,
                                reason=reason,
                                is_edit_tool=False,
                                cycle_approve_mode=lambda mode=None: self.chat_manager.cycle_approve_mode(mode),
                            )
                            set_active_interaction(self.chat_manager, interaction)
                            raise ToolApprovalSuspended(
                                interaction=interaction,
                                tool_id=tool_id,
                                function_name=function_name,
                                tool=tool,
                                arguments=arguments,
                                context=context,
                            )
                        else:
                            result, should_exit, command_executed = handle_command_approval(
                                command, arguments, tool, context, console,
                                thinking_indicator, self.chat_manager.approve_mode,
                                self.debug_mode,
                                cron_job_id=self.cron_job_id,
                                cron_allowlist=self.cron_allowlist,
                                cron_interactive=self.cron_interactive,
                                chat_manager=self.chat_manager)
                            if should_exit:
                                return True, result

                            # Display execute_command output when command actually ran
                            if command_executed:
                                label = build_tool_label(function_name, arguments)
                                label_text = f"[grey]{label}[/grey]"
                                if not self.panel_updater:
                                    console.print(label_text, highlight=False)
                                    console.file.flush()
                                display_tool_feedback(label, result, console, indent=self.is_sub_agent, panel_updater=self.panel_updater)
                            return False, result
                    else:
                        # Other tools with requires_approval can be handled here in the future
                        result = tool.execute(arguments, context)
                else:
                    # No approval required - execute normally
                    # Handle thinking indicator based on tool's terminal policy
                    if policy == TERMINAL_YIELD and thinking_indicator:
                        thinking_indicator.pause()
                        # Force print to clear the status line
                        temp_console = self._get_console()
                        temp_console.print()
                        temp_console.file.flush()
                    
                    # Print subagent label in chat before execution
                    if function_name == "sub_agent" and not self.panel_updater:
                        sa_console = self._get_console()
                        args = arguments if isinstance(arguments, dict) else json.loads(arguments) if arguments else {}
                        query_text = args.get("query", "")
                        sa_console.print("[grey]subagent:[/grey]")
                        sa_console.print(f"[grey]{query_text}[/grey]", highlight=False)
                        sa_console.file.flush()
                    
                    if function_name == "create_file" and self.create_file_handler:
                        result = self.create_file_handler(arguments, context)
                    elif function_name == "select_option" and self._can_suspend_approval():
                        # Suspend select_option to the main prompt toolbar
                        # instead of running a nested prompt_toolkit Application.
                        # Validate questions first (same checks as the tool itself).
                        questions = arguments.get("questions", [])
                        if not isinstance(questions, list) or not questions:
                            result = "exit_code=1\nQuestions must be a non-empty list"
                        else:
                            # Append custom input option (same as tool does)
                            from tools.select_option import CUSTOM_INPUT_OPTION
                            for q in questions:
                                q["options"] = list(q.get("options", [])) + [CUSTOM_INPUT_OPTION]
                            if thinking_indicator:
                                thinking_indicator.stop()
                            from tools.select_option import SelectionPanel
                            from ui.toolbar_interactions import (
                                set_active_interaction,
                                patch_for_active_prompt,
                                SELECTION_RESOLVED_SENTINEL,
                            )
                            panel = SelectionPanel(questions, chat_manager=self.chat_manager)
                            # patch_for_active_prompt wraps finish/cancel with an
                            # is_done() guard so duplicate key events are idempotent.
                            patch_for_active_prompt(panel, SELECTION_RESOLVED_SENTINEL)
                            set_active_interaction(self.chat_manager, panel)
                            raise SelectionSuspended(
                                interaction=panel,
                                tool_id=tool_id,
                                function_name=function_name,
                                tool=tool,
                                arguments=arguments,
                                context=context,
                                questions=questions,
                            )
                        # Validation failure already set result — fall through.
                    else:
                        result = tool.execute(arguments, context)
                    
                    # Resume thinking indicator for yield policy
                    if policy == TERMINAL_YIELD and thinking_indicator:
                        thinking_indicator.resume()

                # Boundary escalation: if the tool result is a path boundary
                # violation, prompt the user to grant session-wide access.
                result_str = str(result)

                # Subagent cancellation — stop the main loop immediately
                # and do NOT append the cancellation result to chat history.
                if function_name == "sub_agent" and result_str.strip().startswith("exit_code=130"):
                    return True, None

                if is_boundary_error(result_str):
                    path_arg = arguments.get("path", arguments.get("path_str", ""))
                    if not path_arg:
                        path_arg = extract_boundary_path(result_str)
                    access_reason = arguments.get("reason", "")
                    if not access_reason.strip():
                        return False, (
                            "exit_code=1\n"
                            "Full filesystem access requires a non-empty reason. "
                            "Retry this tool call with a reason explaining why access outside the project boundary is necessary."
                        )
                    granted = self._boundary_prompt(
                        path_arg, access_reason, thinking_indicator,
                        tool_id=tool_id, function_name=function_name,
                        tool=tool, arguments=arguments, context=context,
                    )
                    if granted:
                        set_full_filesystem_access(True)
                        # Retry with the boundary now lifted
                        result = tool.execute(arguments, context)
                        result_str = str(result)

                # Display result for registry tools
                # Skip display for tools that own their terminal UX (sub_agent, yield-policy tools).
                # sub_agent handles its own panel display; the returned result_str goes to chat history.
                if policy != TERMINAL_YIELD and function_name != "sub_agent":
                    console = self._get_console()
                    if console:
                        # Build label with arguments for better display
                        label = build_tool_label(function_name, arguments)

                        # For task list tools: only show the task list, no label duplication
                        if function_name in ("create_task_list", "complete_task", "show_task_list"):
                            # Extract and format task list directly
                            exit_code = extract_exit_code(result)
                            if exit_code == 0 or exit_code is None:
                                rendered = result
                                if rendered.startswith("exit_code="):
                                    rendered = "\n".join(rendered.splitlines()[1:])
                                _print_or_append(rendered.strip(), console, self.panel_updater, markup=True)
                            else:
                                first_two = "\n".join(result.splitlines()[:2]).strip()
                                _print_or_append(first_two or result.strip(), console, self.panel_updater, markup=False)
                            if not self.panel_updater:
                                console.print()
                        else:
                            # Print label first (like parallel mode)
                            label_text = f"[grey]{label}[/grey]" if not function_name.startswith("web search") else f"[bold #5F9EA0]{label}[/bold #5F9EA0]"
                            if not self.panel_updater:
                                console.print(label_text, highlight=False)
                                console.file.flush()

                            # Then display feedback
                            display_tool_feedback(label, result, console, indent=self.is_sub_agent, panel_updater=self.panel_updater)

                self._check_swarm_completion(function_name)

                return False, result_str
            except (ToolApprovalSuspended, SelectionSuspended, BoundaryApprovalSuspended):
                # Re-raise suspension exceptions so they propagate to
                # _execute_tools_sequential → _handle_tool_calls → run().
                raise
            except Exception as e:
                # If thinking_indicator was paused (TERMINAL_YIELD) and tool
                # raised, resume it so the spinner reappears for the next iteration
                if policy == TERMINAL_YIELD and thinking_indicator:
                    thinking_indicator.resume()
                # Clean up subagent panel state on error
                if function_name == "sub_agent" and panel_to_use is not self.panel_updater:
                    panel_to_use.set_error(str(e))
                return False, f"Error executing tool '{function_name}': {str(e)}"

        return False, f"Error: Unknown tool '{function_name}'."

    def resume_after_approval(self, thinking_indicator=None):
        """Resume the agentic loop after a suspended tool approval is resolved.

        Reads the (action, guidance) tuple from the suspended interaction,
        builds the appropriate tool result message, appends it to history,
        processes any remaining tool calls in the batch, and then continues
        the main while loop.

        Args:
            thinking_indicator: Optional ThinkingIndicator instance.

        Returns:
            "done" on normal completion, "suspended" if another approval
            was handed off.
        """
        exc = self._suspended_exc
        if exc is None:
            return "done"
        self._suspended_exc = None

        interaction = exc.interaction
        result = interaction.result()

        # Clear the active toolbar interaction now that it's resolved.
        # This prevents stale rendering on the next prompt cycle.
        from ui.toolbar_interactions import clear_active_interaction
        clear_active_interaction(self.chat_manager)

        action, guidance = result if isinstance(result, tuple) else (result, None)

        console = self._get_console()

        # ---- Build tool result from approval action -----------------------
        if action == "accept":
            if exc.function_name == "edit_file":
                # Re-execute the edit since it was preview-only before suspension
                from tools.edit import _execute_edit_file
                final_result = _execute_edit_file(
                    path=exc.arguments.get('path'),
                    search=exc.arguments.get('search'),
                    replace=exc.arguments.get('replace'),
                    repo_root=self.repo_root,
                    console=console,
                    gitignore_spec=self.gitignore_spec,
                    context_lines=exc.arguments.get('context_lines', 3),
                    vault_root=vault_root_str(),
                )
                if final_result and isinstance(final_result, str):
                    result_lines = [l for l in final_result.split('\n')
                                   if not l.startswith('exit_code=')]
                    final_result = '\n'.join(result_lines).strip()
                tool_result = str(final_result)
            elif exc.function_name == "execute_command":
                # Execute the command now that it's approved
                tool_result = exc.tool.execute(exc.arguments, exc.context)
                # Display output
                if console:
                    from core.tool_feedback import build_tool_label, display_tool_feedback
                    label = build_tool_label(exc.function_name, exc.arguments)
                    label_text = f"[grey]{label}[/grey]"
                    console.print(label_text, highlight=False)
                    console.file.flush()
                    display_tool_feedback(
                        label, tool_result, console,
                        indent=self.is_sub_agent,
                        panel_updater=self.panel_updater,
                    )
            else:
                tool_result = exc.tool.execute(exc.arguments, exc.context)
            tool_result_str = str(tool_result)
        elif action == "advise":
            tool_result_str = (
                f"exit_code=1\nTool not applied. User advice: {guidance}"
            )
            if console:
                console.print(f"[dim]Not applied. User advice: {guidance}[/dim]")
        else:  # cancel
            tool_result_str = (
                "exit_code=1\nOperation canceled by user. "
                "Do not retry this operation."
            )
            if console:
                console.print("[dim]Operation canceled by user.[/dim]")

        # ---- Append tool result message ----------------------------------
        if isinstance(tool_result_str, str):
            tool_msg = {
                "role": "tool",
                "tool_call_id": exc.tool_id,
                "content": tool_result_str,
            }
        else:
            # Rich Text object from edit_file
            tool_msg = {
                "role": "tool",
                "tool_call_id": exc.tool_id,
                "content": f"exit_code=0\n{str(tool_result_str)}",
            }
        self.chat_manager.messages.append(tool_msg)
        self.chat_manager.log_message(tool_msg)

        # ---- Process remaining tool calls in the batch -------------------
        if self._process_remaining_tool_calls(exc.remaining_tool_calls, thinking_indicator):
            return "done"

        # ---- Compact tool results and continue the while loop ------------
        self.chat_manager.compact_tool_results(skip_token_update=True)

        tools_for_mode = self._get_effective_tools(
            allowed_tools=getattr(self, "_current_allowed_tools", None),
            allow_active_plugins=getattr(self, "_current_allow_active_plugins", False),
        )
        self.chat_manager._update_context_tokens(tools_for_mode)
        self.chat_manager.ensure_context_fits(console=self.console)

        # Re-enter the main while loop (without appending a new user message).
        return self._continue_loop(thinking_indicator)

    def _process_remaining_tool_calls(self, remaining, thinking_indicator=None):
        """Process the rest of a suspended tool-call batch.

        Returns True if the caller should stop. If another interactive action
        suspends, stores that new suspension and returns True so control yields
        cleanly to the main prompt.
        """
        if not remaining:
            return False

        for i, tool_call in enumerate(remaining):
            tool_id = tool_call["id"]
            try:
                should_exit, tool_result = self._process_single_tool_call(
                    tool_call, thinking_indicator
                )
            except (ToolApprovalSuspended, SelectionSuspended, BoundaryApprovalSuspended) as e:
                e.remaining_tool_calls = remaining[i + 1:]
                self._suspended_exc = e
                return True

            if should_exit:
                if tool_result is not None and tool_result is not False:
                    self._append_tool_result(tool_id, tool_result)
                return True

            if tool_result is not None and tool_result is not False:
                self._append_tool_result(tool_id, tool_result)

        return False

    def _append_tool_result(self, tool_id, tool_result):
        content = (
            f"exit_code=0\n{str(tool_result)}"
            if hasattr(tool_result, '__rich__')
            else str(tool_result)
        )
        tool_msg = {
            "role": "tool",
            "tool_call_id": tool_id,
            "content": content,
        }
        self.chat_manager.messages.append(tool_msg)
        self.chat_manager.log_message(tool_msg)

    def resume_after_selection(self, thinking_indicator=None):
        """Resume the agentic loop after a suspended select_option is resolved.

        Reads the selection result from the suspended interaction, builds
        the appropriate tool result message (matching the format the
        select_option tool normally returns), appends it to history,
        processes any remaining tool calls, and continues the main loop.

        Args:
            thinking_indicator: Optional ThinkingIndicator instance.

        Returns:
            "done" on normal completion, "suspended" if another interaction
            was handed off.
        """
        exc = self._suspended_exc
        if exc is None:
            return "done"
        self._suspended_exc = None

        interaction = exc.interaction

        # Clear the active toolbar interaction now that it's resolved.
        from ui.toolbar_interactions import clear_active_interaction
        clear_active_interaction(self.chat_manager)

        # Build the tool result from the selection outcome.
        if interaction.was_cancelled():
            tool_result_str = "exit_code=1\nUser canceled selection"
            console = self._get_console()
            if console:
                console.print("[dim]Selection canceled by user.[/dim]")
        else:
            raw_result = interaction.result()
            # Replicate select_option's formatting logic
            if raw_result is None:
                tool_result_str = "exit_code=1\nUser canceled selection"
            elif isinstance(raw_result, str):
                tool_result_str = f"exit_code=0\n{raw_result}"
            elif isinstance(raw_result, list):
                formatted = []
                for r in raw_result:
                    if isinstance(r, list):
                        formatted.append(', '.join(str(v) for v in r))
                    else:
                        formatted.append(str(r))
                tool_result_str = f"exit_code=0\n{', '.join(formatted)}"
            else:
                tool_result_str = f"exit_code=0\n{raw_result}"
            console = self._get_console()
            if console:
                selected_text = tool_result_str.split("\n", 1)[1].strip()
                console.print(f"[dim]Selected: {selected_text}[/dim]", highlight=False)

        # Append tool result message
        tool_msg = {
            "role": "tool",
            "tool_call_id": exc.tool_id,
            "content": tool_result_str,
        }
        self.chat_manager.messages.append(tool_msg)
        self.chat_manager.log_message(tool_msg)

        # Process remaining tool calls in the batch
        if self._process_remaining_tool_calls(exc.remaining_tool_calls, thinking_indicator):
            return "done"

        # Compact and continue the while loop
        self.chat_manager.compact_tool_results(skip_token_update=True)

        tools_for_mode = self._get_effective_tools(
            allowed_tools=getattr(self, "_current_allowed_tools", None),
            allow_active_plugins=getattr(self, "_current_allow_active_plugins", False),
        )
        self.chat_manager._update_context_tokens(tools_for_mode)
        self.chat_manager.ensure_context_fits(console=self.console)

        return self._continue_loop(thinking_indicator)

    def resume_after_boundary(self, thinking_indicator=None):
        """Resume the agentic loop after a suspended boundary approval is resolved.

        Reads the (action, _) tuple from the suspended interaction.
        If the user granted access, sets full filesystem access and
        re-executes the original tool with the boundary lifted.
        If denied, uses the original boundary error as the tool result.

        Args:
            thinking_indicator: Optional ThinkingIndicator instance.

        Returns:
            "done" on normal completion, "suspended" if another interaction
            was handed off.
        """
        exc = self._suspended_exc
        if exc is None:
            return "done"
        self._suspended_exc = None

        interaction = exc.interaction
        result = interaction.result()

        # Clear the active toolbar interaction now that it's resolved.
        from ui.toolbar_interactions import clear_active_interaction
        clear_active_interaction(self.chat_manager)

        action, _ = result if isinstance(result, tuple) else (result, None)
        console = self._get_console()

        # ---- Build tool result from boundary decision --------------------
        if action == "accept":
            set_full_filesystem_access(True)
            if console:
                console.print("[yellow]Full filesystem access granted[/yellow]\n")
            # Re-execute the tool with the boundary now lifted
            tool_result = exc.tool.execute(exc.arguments, exc.context)
            tool_result_str = str(tool_result)
            # Display output
            if console:
                from core.tool_feedback import build_tool_label, display_tool_feedback
                label = build_tool_label(exc.function_name, exc.arguments)
                label_text = f"[grey]{label}[/grey]"
                console.print(label_text, highlight=False)
                console.file.flush()
                display_tool_feedback(
                    label, tool_result_str, console,
                    indent=self.is_sub_agent,
                    panel_updater=self.panel_updater,
                )
        else:  # cancel
            tool_result_str = (
                "exit_code=1\n"
                "Full filesystem access denied by user. "
                "Do not retry this operation on this path."
            )
            if console:
                console.print("[dim]Full filesystem access denied by user.[/dim]")

        # ---- Append tool result message ----------------------------------
        tool_msg = {
            "role": "tool",
            "tool_call_id": exc.tool_id,
            "content": tool_result_str,
        }
        self.chat_manager.messages.append(tool_msg)
        self.chat_manager.log_message(tool_msg)

        # ---- Process remaining tool calls in the batch -------------------
        if self._process_remaining_tool_calls(exc.remaining_tool_calls, thinking_indicator):
            return "done"

        # ---- Compact tool results and continue the while loop ------------
        self.chat_manager.compact_tool_results(skip_token_update=True)

        tools_for_mode = self._get_effective_tools(
            allowed_tools=getattr(self, "_current_allowed_tools", None),
            allow_active_plugins=getattr(self, "_current_allow_active_plugins", False),
        )
        self.chat_manager._update_context_tokens(tools_for_mode)
        self.chat_manager.ensure_context_fits(console=self.console)

        return self._continue_loop(thinking_indicator)

    def _check_swarm_completion(self, fn_name: str) -> None:
        """Mark swarm complete if admin just finished the last task."""
        if fn_name == "complete_task" and getattr(self.chat_manager, "swarm_admin_mode", False):
            task_list = getattr(self.chat_manager, "task_list", None) or []
            if task_list and all(t.get("completed") for t in task_list):
                self.chat_manager.swarm_complete = True

    def _drain_and_inject_swarm_messages(self):
        """Drain swarm inject queue, evict old [AUTO-TURN] messages, inject new ones.

        Injects mid-turn swarm events (worker approvals, completions) as
        synthetic user messages prefixed with [AUTO-TURN].  Before injecting,
        evicts the oldest [AUTO-TURN] messages, capping them at 5 to prevent
        unbounded context growth in long-running swarm sessions.
        """
        inject_queue = getattr(self.chat_manager, '_swarm_inject_queue', None)
        if inject_queue is None:
            return

        pending_prompts = []
        while not inject_queue.empty():
            try:
                pending_prompts.append(inject_queue.get_nowait())
            except Exception:
                break

        if not pending_prompts:
            return

        # Evict old [AUTO-TURN] messages, keeping the last N (cap at 5 total)
        AUTO_TURN_TAG = "[AUTO-TURN]"
        MAX_AUTO_TURN = 5
        auto_turn_indices = [
            i for i, msg in enumerate(self.chat_manager.messages)
            if isinstance(msg.get("content"), str)
            and msg["content"].startswith(AUTO_TURN_TAG)
        ]
        if len(auto_turn_indices) >= MAX_AUTO_TURN:
            # Remove the oldest, leaving room for the new injection
            to_remove = auto_turn_indices[:-(MAX_AUTO_TURN - 1)]
            for idx in reversed(to_remove):
                del self.chat_manager.messages[idx]
            if self.debug_mode:
                self.console.print(
                    "[dim]Evicted %d old [AUTO-TURN] message(s)[/dim]"
                    % len(to_remove)
                )

        inject_content = AUTO_TURN_TAG + "\n" + "\n\n---\n\n".join(pending_prompts)
        inject_msg = {"role": "user", "content": inject_content}
        self.chat_manager.messages.append(inject_msg)
        self.chat_manager.log_message(inject_msg)
        if self.debug_mode:
            self.console.print(
                "[dim]Injected %d swarm event(s) mid-turn[/dim]"
                % len(pending_prompts)
            )

    def _continue_loop(self, thinking_indicator=None):
        """Internal: continue the main orchestration loop from its current state.

        Does NOT append a new user message — picks up where ``run()`` left off.
        """
        from tools.helpers.base import ToolRegistry

        try:
            while True:
                if self._cancel_requested():
                    raise AgentTurnCancelled()

                evicted = ToolRegistry.decrement_plugin_ttls()
                if evicted and self.debug_mode:
                    self.console.print(
                        f"[dim]Plugins evicted (TTL expired): {evicted}[/dim]"
                    )

                # Mid-turn swarm inbox injection
                self._drain_and_inject_swarm_messages()

                response = self._get_llm_response(
                    allowed_tools=getattr(self, "_current_allowed_tools", None),
                    allow_active_plugins=getattr(
                        self, "_current_allow_active_plugins", False
                    ),
                )
                if response is None:
                    return "done"
                if self._cancel_requested():
                    raise AgentTurnCancelled()

                self.chat_manager.maybe_auto_compact()

                tool_calls = response.get("tool_calls")
                if not tool_calls:
                    if self._handle_final_response(response, thinking_indicator):
                        return "done"
                else:
                    should_exit = self._handle_tool_calls(
                        response,
                        thinking_indicator,
                        allowed_tools=getattr(self, "_current_allowed_tools", None),
                        allow_active_plugins=getattr(
                            self, "_current_allow_active_plugins", False
                        ),
                    )
                    if should_exit:
                        return "done"
        except (ToolApprovalSuspended, SelectionSuspended, BoundaryApprovalSuspended) as e:
            self._suspended_exc = e
            return "suspended"
        except AgentTurnCancelled:
            return "cancelled"
        except KeyboardInterrupt:
            raise

def agentic_answer(chat_manager, user_input: str | list[dict[str, Any]], console, repo_root, rg_exe_path, debug_mode, thinking_indicator=None):
    """Main agent loop using OpenAI-style function calling.

    This is a convenience wrapper that creates an AgenticOrchestrator
    and runs it with the provided parameters.

    Args:
        chat_manager: ChatManager instance
        user_input: User's input message content
        console: Rich console for output
        repo_root: Path to repository root
        rg_exe_path: Path to rg.exe
        debug_mode: Whether to show debug output
        thinking_indicator: Optional ThinkingIndicator instance

    Returns:
        "done" on normal completion, "suspended" if a tool approval was
        handed off to the main prompt.  The orchestrator is stored on
        ``chat_manager._agentic_orchestrator`` for later resume.
    """
    orchestrator = AgenticOrchestrator(
        chat_manager=chat_manager,
        repo_root=repo_root,
        rg_exe_path=rg_exe_path,
        console=console,
        debug_mode=debug_mode,
    )
    result = orchestrator.run(user_input, thinking_indicator)
    if result == "suspended":
        # Stash the orchestrator so the main loop can resume it.
        chat_manager._agentic_orchestrator = orchestrator
    return result
