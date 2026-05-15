"""Sub-agent for delegated tasks.

Uses existing AgenticOrchestrator with isolated message context
and read-only tools to execute generic delegated tasks.
"""

from pathlib import Path

from core.chat_manager import ChatManager
from exceptions import LLMError
from llm.prompts import build_sub_agent_prompt
from utils.settings import context_settings, sub_agent_settings


# Effective hard limit: the minimum of what the sub-agent config allows and
# what the model/context window actually supports. This prevents sub-agent
# API calls from exceeding the model's context window when sub_agent_settings
# is configured higher than context_settings.hard_limit_tokens.
_effective_hard_limit_tokens = min(
    sub_agent_settings.hard_limit_tokens,
    context_settings.hard_limit_tokens,
)

# Effective billed-token limit: caps cumulative API token usage per sub-agent
# to prevent runaway costs from many small calls that stay under the context limit.
_effective_billed_limit_tokens = sub_agent_settings.billed_token_limit


def _last_assistant_content(messages: list[dict], max_chars: int = 60_000) -> str:
    """Extract content from the last assistant message with non-empty content.

    If *max_chars* is given the result is truncated to that many characters
    (keeping the tail, which tends to contain the most recent findings).
    """
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and msg.get("content"):
            content = msg["content"].strip()
            if max_chars and len(content) > max_chars:
                content = content[-max_chars:]
            return content
    return ""


class HardLimitExceeded(Exception):
    """Raised when the sub-agent hits its context hard limit (prevents API errors)."""
    pass


class SubAgentCancelled(Exception):
    """Raised when the sub-agent is cancelled by the user (Ctrl+C)."""
    pass


def _format_messages_summary(messages, reason: str = "Hard Limit Reached", max_chars: int = 60_000) -> str:
    """Format a bounded overflow summary for the parent agent.

    Returning a 500k-token dump as one tool result can overflow the parent
    conversation, make logs/renderers unusable, and leak hidden sub-agent
    history into user-visible output.  This summary preserves the useful
    recent assistant findings and tool-call breadcrumbs while staying bounded.
    """
    lines = [
        f"## Sub-agent stopped before completion ({reason})",
        "",
        "The delegated sub-agent exceeded its token budget. The full internal history was not returned because it was too large. Use the partial findings and recent activity below; continue with focused searches if more detail is needed.",
        "",
    ]

    assistant_snippets = []
    tool_breadcrumbs = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = (msg.get("content") or "").strip()
        tool_calls = msg.get("tool_calls") or []

        if role == "assistant" and content:
            assistant_snippets.append(content)
        for tc in tool_calls:
            fn = tc.get("function", {})
            tool_breadcrumbs.append(f"- `{fn.get('name', '?')}` — `{fn.get('arguments', '')}`")

    if assistant_snippets:
        lines.extend(["### Recent assistant findings", ""])
        remaining = max_chars // 2
        for snippet in reversed(assistant_snippets[-6:]):
            if remaining <= 0:
                break
            clipped = snippet[-remaining:]
            lines.extend([clipped, ""])
            remaining -= len(clipped)

    if tool_breadcrumbs:
        lines.extend(["### Recent tool activity", ""])
        lines.extend(tool_breadcrumbs[-40:])
        lines.append("")

    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[-max_chars:]
        result = (
            f"## Sub-agent stopped before completion ({reason})\n\n"
            "Earlier overflow summary content was truncated to keep the parent context safe.\n\n"
            f"{result}"
        )
        # Truncate again after prepending the header so the final
        # output stays within max_chars.
        if len(result) > max_chars:
            result = result[-max_chars:]
    return result


def _configure_compaction():
    """Create a ChatManager with compaction settings from config.

    Returns:
        ChatManager: A new ChatManager instance with compaction configured
    """
    if sub_agent_settings.enable_compaction:
        return ChatManager(compact_trigger_tokens=sub_agent_settings.compact_trigger_tokens)
    else:
        cm = ChatManager(compact_trigger_tokens=None)
        cm._compaction_disabled = True
        return cm


def _inject_system_prompt(chat_manager, sub_agent_type: str = "research", diff_content: str | None = None):
    """Build sub-agent prompt and inject it.

    Token usage is reported live by the wrapper in run_sub_agent(),
    so the system prompt is kept clean.

    Args:
        chat_manager: ChatManager instance to configure
        sub_agent_type: Type of sub-agent ('research' or 'review').
        diff_content: Optional git diff to embed in the system prompt (review mode).
    """
    base_prompt = build_sub_agent_prompt(
        sub_agent_type=sub_agent_type,
        hard_limit_tokens=_effective_hard_limit_tokens,
        diff_content=diff_content,
    )
    chat_manager.replace_messages([{"role": "system", "content": base_prompt}], sync_log=False)


def _load_codebase_map(chat_manager):
    """Load agents.md codebase map into sub-agent context if available.

    Args:
        chat_manager: ChatManager instance to add context to
    """
    agents_path = Path.cwd() / "agents.md"
    if agents_path.exists():
        map_content = agents_path.read_text(encoding="utf-8").strip()
        user_msg = (
            "Here is the codebase map for this project. "
            "This provides an overview of the repository structure and file purposes. "
            "Use this as a reference when exploring the codebase.\n\n"
            f"## Codebase Map (auto-generated from agents.md)\n\n{map_content}"
        )
        chat_manager.add_message({"role": "user", "content": user_msg})


def _configure_isolation(chat_manager):
    """Apply isolation settings for sub-agent context.

    Disables conversation logging.

    Args:
        chat_manager: ChatManager instance to configure
    """
    chat_manager.markdown_logger = None


def _create_chat_manager(sub_agent_type: str = "research", diff_content: str | None = None):
    """Create a fresh ChatManager instance for sub-agent use.

    Orchestrates compaction, prompt injection, codebase map loading,
    and isolation configuration.

    Args:
        sub_agent_type: Type of sub-agent ('research' or 'review').
        diff_content: Optional git diff to embed in the system prompt (review mode).

    Returns:
        ChatManager: A new ChatManager instance with pre-configured system prompt
    """
    chat_manager = _configure_compaction()
    _inject_system_prompt(chat_manager, sub_agent_type=sub_agent_type, diff_content=diff_content)
    _load_codebase_map(chat_manager)
    _configure_isolation(chat_manager)
    return chat_manager


def run_sub_agent(
    task_query: str,
    repo_root: Path,
    rg_exe_path: str,
    console=None,
    panel_updater=None,
    sub_agent_type: str = "research",
    initial_context: str = None,
    cancel_event=None,
    diff_content: str | None = None,
) -> dict:
    """Run sub-agent using existing AgenticOrchestrator for delegated tasks.

    Args:
        task_query: Generic task query to execute (e.g., "Read file config.json")
        repo_root: Repository root path
        rg_exe_path: Path to rg executable
        console: Optional Rich console for output
        panel_updater: Optional SubAgentPanel for live panel updates
        sub_agent_type: Type of sub-agent ('research' or 'review').
        initial_context: Optional string injected as context before the task query
            (e.g., conversation history for /ask). Not used for review mode —
            use diff_content instead.
        cancel_event: Optional event to signal cancellation.
        diff_content: Optional git diff embedded in the system prompt (review mode).
            More efficient than initial_context because it avoids wasting a
            user-message turn on raw diff text.

    Returns:
        Dict with:
            - 'result': Formatted markdown string (goes into chat history)
            - 'usage': Usage data for billing
            - 'error': Error message if failed (None if success)
    """
    # Validate panel_updater type if provided
    if panel_updater is not None and not hasattr(panel_updater, 'append'):
        panel_updater = None

    # If no panel_updater provided, create a simple no-op one
    if panel_updater is None:
        from tools.sub_agent import SimplePanelUpdater
        panel_updater = SimplePanelUpdater(console)

    # Create fresh ChatManager for sub-agent
    temp_chat_manager = _create_chat_manager(sub_agent_type=sub_agent_type, diff_content=diff_content)

    # Inject initial context as a user message if provided (used by /ask, not /review)
    if initial_context:
        temp_chat_manager.add_message(
            {"role": "user", "content": initial_context}
        )

    # Import here to avoid circular import with core.agentic
    from core.agentic import AgenticOrchestrator
    # Create orchestrator (reuses existing implementation)
    orchestrator = AgenticOrchestrator(
        chat_manager=temp_chat_manager,
        repo_root=repo_root,
        rg_exe_path=rg_exe_path,
        console=console,
        debug_mode=False,
        suppress_result_display=True,
        is_sub_agent=True,
        panel_updater=panel_updater,
    )

    # Wire the inner orchestrator's cancel event to the parent's subagent
    # cancel event.  By default the inner orchestrator snapshots
    # temp_chat_manager.get_agent_cancel_event() — a fresh event that is
    # never set.  Replacing it with the parent's subagent cancel event
    # makes the inner loop's _cancel_requested() checks (top of while,
    # between tools, before/after LLM calls) all respond to Ctrl+C.
    if cancel_event is not None:
        orchestrator.set_cancel_event(cancel_event)

    # Wrap orchestrator._get_llm_response to check context hard limit
    # before each LLM call, preventing API context_length_exceeded errors.
    original_get_llm_response = orchestrator._get_llm_response

    def _get_llm_response_with_hard_limit(allowed_tools=None, allow_active_plugins=False):
        """Wrapper to check context hard limit before each LLM call."""
        if cancel_event and cancel_event.is_set():
            raise SubAgentCancelled("Sub-agent cancelled by user.")

        # Force recount before reading — tool results or initial_context
        # may have been added since the last token update.
        temp_chat_manager._update_context_tokens(force=True)
        tt = temp_chat_manager.token_tracker

        # Check hard token limit before making LLM call.
        # Uses current_context_tokens (prompt size) to catch
        # prompt-length-over-limit errors before they hit the API.
        if tt.current_context_tokens >= _effective_hard_limit_tokens:
            raise HardLimitExceeded(
                f"Sub-agent context hard limit exceeded: "
                f"{tt.current_context_tokens:,} / {_effective_hard_limit_tokens:,} tokens."
            )

        # Check cumulative billed-token limit to prevent runaway costs.
        # A sub-agent can make many small calls that each stay under the
        # context hard limit but accumulate large API costs over time.
        if tt.conv_total_tokens >= _effective_billed_limit_tokens:
            raise HardLimitExceeded(
                f"Sub-agent billed-token limit exceeded: "
                f"{tt.conv_total_tokens:,} / {_effective_billed_limit_tokens:,} tokens."
            )

        # Update panel with live token counts
        conv_length = tt.current_context_tokens
        total_billed = tt.conv_total_tokens
        if hasattr(panel_updater, 'token_info'):
            panel_updater.token_info = f"{conv_length:,} curr | {total_billed:,} total"
            panel_updater.append("")  # Refresh panel title

        response = original_get_llm_response(
            allowed_tools=allowed_tools,
            allow_active_plugins=allow_active_plugins,
        )

        # Post-call cancellation check: if user pressed Ctrl+C while the HTTP
        # call was in flight, discard the late response before it reaches the
        # orchestrator loop (which would otherwise process it and call tools).
        if cancel_event and cancel_event.is_set():
            raise SubAgentCancelled("Sub-agent cancelled by user.")

        return response

    # Apply patch once, before the orchestrator loop starts
    orchestrator._get_llm_response = _get_llm_response_with_hard_limit

    cancelled = False
    hard_limit_exceeded = False

    try:
        # Run sub-agent task
        orchestrator.run(
            task_query,
            thinking_indicator=None,
            allowed_tools=sub_agent_settings.allowed_tools,
            allow_active_plugins=sub_agent_settings.allow_active_plugins,
        )
        # Check cancellation after run completes (may have been signalled
        # during a non-LLM phase like tool execution).
        if cancel_event and cancel_event.is_set():
            raise SubAgentCancelled("Sub-agent cancelled by user.")
    except HardLimitExceeded:
        hard_limit_exceeded = True
    except SubAgentCancelled:
        cancelled = True
    except LLMError as e:
        error_str = str(e).lower()
        if "context_length_exceeded" in error_str or "maximum context length" in error_str:
            hard_limit_exceeded = True
        else:
            return {
                "result": "",
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0
                },
                "model": temp_chat_manager.client.model,
                "error": str(e)
            }
    except Exception as e:
        import traceback
        error_details = f"{e}\n\nTraceback:\n{traceback.format_exc()}"
        return {
            "result": "",
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0
            },
            "model": "",
            "error": error_details
        }
    if cancelled:
        tt = temp_chat_manager.token_tracker
        usage = {
            "prompt_tokens": tt.total_prompt_tokens,
            "completion_tokens": tt.total_completion_tokens,
            "total_tokens": tt.total_tokens,
            "context_tokens": tt.current_context_tokens,
            "cache_read_input_tokens": tt.total_cache_read_tokens,
            "cache_creation_input_tokens": tt.total_cache_creation_tokens,
        }
        delta_cost = tt.total_actual_cost + tt.total_estimated_cost
        if delta_cost > 0:
            usage["cost"] = delta_cost
        return {
            "result": "",
            "usage": usage,
            "model": temp_chat_manager.client.model,
            "error": None,
            "cancelled": True,
        }

    # Get final token usage
    delta_prompt = temp_chat_manager.token_tracker.total_prompt_tokens
    delta_completion = temp_chat_manager.token_tracker.total_completion_tokens
    delta_total = temp_chat_manager.token_tracker.total_tokens
    tt = temp_chat_manager.token_tracker
    delta_cost = tt.total_actual_cost + tt.total_estimated_cost

    if hard_limit_exceeded:
        result = _format_messages_summary(temp_chat_manager.messages, "Token Budget Exhausted")
    else:
        result = _last_assistant_content(temp_chat_manager.messages)

    usage = {
        "prompt_tokens": delta_prompt,
        "completion_tokens": delta_completion,
        "total_tokens": delta_total,
        "context_tokens": tt.current_context_tokens,
        "cache_read_input_tokens": tt.total_cache_read_tokens,
        "cache_creation_input_tokens": tt.total_cache_creation_tokens,
    }
    if delta_cost > 0:
        usage["cost"] = delta_cost

    return {
        "result": result,
        "usage": usage,
        "model": temp_chat_manager.client.model,
        "error": None,
        "hard_limit_exceeded": hard_limit_exceeded,
        "hard_limit_tokens": _effective_hard_limit_tokens,
        "context_tokens": tt.current_context_tokens,
        "context_dumped": hard_limit_exceeded,
    }
