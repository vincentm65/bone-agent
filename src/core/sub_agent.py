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


class HardLimitExceeded(Exception):
    """Raised when the sub-agent hits its hard token limit."""
    pass


class BilledLimitExceeded(Exception):
    """Raised when the sub-agent hits its cumulative billed token limit."""
    pass


class SubAgentCancelled(Exception):
    """Raised when the sub-agent is cancelled by the user (Ctrl+C)."""
    pass


class PreflightContextOverflow(Exception):
    """Raised when initial sub-agent context already exceeds hard_limit_tokens."""
    pass


def _format_messages_dump(messages) -> str:
    """Format sub-agent message history as a markdown dump.

    Args:
        messages: List of message dicts from the sub-agent ChatManager.

    Returns:
        Markdown string with the full conversation context.
    """
    lines = [
        "## Sub-Agent Context Dump (Hard Limit Reached)",
        "",
        "The sub-agent exceeded its hard token limit. Below is the full, unabridged context of its investigation. No summary was produced.",
        "",
        "---",
        "",
    ]
    for i, msg in enumerate(messages):
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        tool_calls = msg.get("tool_calls")
        tool_call_id = msg.get("tool_call_id")

        if tool_call_id:
            lines.append(f"### Message {i} — tool result ({tool_call_id})")
        elif tool_calls:
            lines.append(f"### Message {i} — assistant tool calls")
            for tc in tool_calls:
                fn = tc.get("function", {})
                lines.append(f"- `{fn.get('name', '?')}` — `{fn.get('arguments', '')}`")
        else:
            lines.append(f"### Message {i} — {role}")

        if content:
            # Truncate large content to avoid blowing out the main agent's context
            max_chars = 4000
            if len(content) > max_chars:
                content = content[:max_chars] + f"\n\n... (truncated, {len(content) - max_chars:,} chars omitted)"
            lines.append(content)
        lines.append("")
    return "\n".join(lines)


def _configure_compaction():
    """Create a ChatManager with compaction settings from config.

    Returns:
        ChatManager: A new ChatManager instance with compaction configured
    """
    if sub_agent_settings.enable_compaction:
        return ChatManager(compact_trigger_tokens=sub_agent_settings.compact_trigger_tokens)
    else:
        return ChatManager(compact_trigger_tokens=None)


def _inject_system_prompt(chat_manager, sub_agent_type: str = "research"):
    """Build sub-agent prompt and inject it.

    Token usage is reported live by the wrapper in run_sub_agent(),
    so the system prompt is kept clean.

    Args:
        chat_manager: ChatManager instance to configure
        sub_agent_type: Type of sub-agent ('research' or 'review').
    """
    base_prompt = build_sub_agent_prompt(
        sub_agent_type=sub_agent_type,
        soft_limit_tokens=sub_agent_settings.soft_limit_tokens,
        hard_limit_tokens=_effective_hard_limit_tokens,
    )
    chat_manager.messages = [{"role": "system", "content": base_prompt}]


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
        chat_manager.messages.append({"role": "user", "content": user_msg})


def _configure_isolation(chat_manager):
    """Apply isolation settings for sub-agent context.

    Disables conversation logging.

    Args:
        chat_manager: ChatManager instance to configure
    """
    chat_manager.markdown_logger = None


def _create_chat_manager(sub_agent_type: str = "research"):
    """Create a fresh ChatManager instance for sub-agent use.

    Orchestrates compaction, prompt injection, codebase map loading,
    and isolation configuration.

    Args:
        sub_agent_type: Type of sub-agent ('research' or 'review').

    Returns:
        ChatManager: A new ChatManager instance with pre-configured system prompt
    """
    chat_manager = _configure_compaction()
    chat_manager._compaction_disabled = True
    _inject_system_prompt(chat_manager, sub_agent_type=sub_agent_type)
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
            (e.g., a git diff for review mode).

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
    temp_chat_manager = _create_chat_manager(sub_agent_type=sub_agent_type)

    # Inject initial context as a user/assistant exchange if provided
    if initial_context:
        temp_chat_manager.messages.append(
            {"role": "user", "content": initial_context}
        )

    # Preflight check: if initial context already exceeds the hard limit,
    # bail out before making any API call. Compaction is disabled for
    # sub-agents, so ensure_context_fits() would only warn — it cannot
    # reduce the context. We must catch this here to avoid a guaranteed
    # context-length error from the API.
    temp_chat_manager._update_context_tokens()
    preflight_tokens = temp_chat_manager.token_tracker.current_context_tokens
    if preflight_tokens >= _effective_hard_limit_tokens:
        return {
            "result": "",
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "context_tokens": preflight_tokens,
            },
            "model": temp_chat_manager.client.model,
            "error": None,
            "preflight_overflow": True,
            "preflight_tokens": preflight_tokens,
            "hard_limit": _effective_hard_limit_tokens,
        }

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
        force_parallel_execution=True  # Enable parallel execution for read-only tools
    )

    # Wrap orchestrator._get_llm_response to check hard token limit and
    # wrap client.chat_completion once (outside the loop) to inject live
    # token feedback as a system message — avoids per-call monkey-patching
    # and eliminates any re-entrancy risk.
    original_get_llm_response = orchestrator._get_llm_response
    original_chat_completion = temp_chat_manager.client.chat_completion

    _soft_limit_warned = False
    _billed_warning_sent = False

    def _chat_completion_with_token_hint(messages, **kwargs):
        """Inject one-time limit warnings when thresholds are crossed. Passes messages
        through unchanged on normal turns to keep the message prefix stable for caching."""
        nonlocal _soft_limit_warned, _billed_warning_sent
        tt = temp_chat_manager.token_tracker
        warnings = []

        if not _soft_limit_warned and tt.current_context_tokens >= sub_agent_settings.soft_limit_tokens:
            _soft_limit_warned = True
            warnings.append(
                f"WARNING: You have exceeded the current-context soft token limit "
                f"({tt.current_context_tokens:,} / {sub_agent_settings.soft_limit_tokens:,}). "
                "STOP exploring and return your findings immediately. Do NOT call any more tools."
            )

        if not _billed_warning_sent and tt.conv_total_tokens >= sub_agent_settings.billed_warning_tokens:
            _billed_warning_sent = True
            warnings.append(
                f"WARNING: You have exceeded the cumulative billed token warning limit "
                f"({tt.conv_total_tokens:,} / {sub_agent_settings.billed_warning_tokens:,}). "
                "This sub-agent may be running away. STOP exploring and return your findings immediately. "
                "Do NOT call any more tools."
            )

        if warnings:
            warning_msg = {"role": "system", "content": "\n".join(warnings)}
            return original_chat_completion([warning_msg, *messages], **kwargs)

        return original_chat_completion(messages, **kwargs)

    def _get_llm_response_with_hard_limit(allowed_tools=None, allow_active_plugins=False):
        """Wrapper to check context and billed token limits and update panel state."""
        if cancel_event and cancel_event.is_set():
            raise SubAgentCancelled("Sub-agent cancelled by user.")

        tt = temp_chat_manager.token_tracker

        # Check hard token limit before making LLM call
        # Use current_context_tokens (prompt size) not total_tokens (cumulative billing)
        # to catch prompt-length-over-limit errors before they hit the API.
        if tt.current_context_tokens >= _effective_hard_limit_tokens:
            raise HardLimitExceeded(
                f"Sub-agent hard token limit exceeded: "
                f"{tt.current_context_tokens:,} / {_effective_hard_limit_tokens:,} tokens."
            )

        # Check cumulative billed tokens to stop runaway sub-agents even when
        # current context remains below the prompt-size hard limit.
        #
        # Note: the billed warning is injected by _chat_completion_with_token_hint
        # on the next chat_completion call. This hard stop runs before each LLM
        # response, so once we hit the billed hard limit the warning may never be
        # delivered if no further chat_completion call is made.
        if tt.conv_total_tokens >= sub_agent_settings.billed_hard_limit_tokens:
            raise BilledLimitExceeded(
                f"Sub-agent billed token limit exceeded: "
                f"{tt.conv_total_tokens:,} / {sub_agent_settings.billed_hard_limit_tokens:,} tokens."
            )

        # Update panel with live token counts
        # Order: conversation length (current context) first, total tokens billed second
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

    # Apply both patches once, before the orchestrator loop starts
    orchestrator._get_llm_response = _get_llm_response_with_hard_limit
    temp_chat_manager.client.chat_completion = _chat_completion_with_token_hint

    hard_limit_exceeded = False
    billed_limit_exceeded = False
    cancelled = False

    # Check cancellation before starting
    if cancel_event and cancel_event.is_set():
        tt = temp_chat_manager.token_tracker
        return {
            "result": "",
            "usage": {
                "prompt_tokens": tt.total_prompt_tokens,
                "completion_tokens": tt.total_completion_tokens,
                "total_tokens": tt.total_tokens,
                "context_tokens": tt.current_context_tokens,
                "cache_read_input_tokens": tt.total_cache_read_tokens,
                "cache_creation_input_tokens": tt.total_cache_creation_tokens,
            },
            "model": temp_chat_manager.client.model,
            "error": None,
            "cancelled": True,
        }

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
    except BilledLimitExceeded:
        billed_limit_exceeded = True
    except SubAgentCancelled:
        cancelled = True
    except LLMError as e:
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
    finally:
        # Restore originals
        temp_chat_manager.client.chat_completion = original_chat_completion

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

    # Get final token usage (no need for delta calculation on fresh instance)
    delta_prompt = temp_chat_manager.token_tracker.total_prompt_tokens
    delta_completion = temp_chat_manager.token_tracker.total_completion_tokens
    delta_total = temp_chat_manager.token_tracker.total_tokens
    tt = temp_chat_manager.token_tracker
    delta_cost = tt.total_actual_cost + tt.total_estimated_cost

    if hard_limit_exceeded and sub_agent_settings.dump_context_on_hard_limit:
        result = _format_messages_dump(temp_chat_manager.messages)
    else:
        # Extract final response (last assistant message with content)
        final_content = ""
        for msg in reversed(temp_chat_manager.messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                final_content = msg["content"].strip()
                break

        if billed_limit_exceeded:
            prefix = (
                f"Sub-agent hit the cumulative token budget "
                f"({tt.conv_total_tokens:,} / {sub_agent_settings.billed_hard_limit_tokens:,} tokens burned). "
                "The findings below are partial — the sub-agent was stopped mid-task. "
                "Review what it found so far and decide whether to continue with a follow-up query."
            )
            result = f"{prefix}\n\n{final_content}" if final_content else prefix
        else:
            result = final_content

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
        "billed_limit_exceeded": billed_limit_exceeded,
        "billed_hard_limit_tokens": sub_agent_settings.billed_hard_limit_tokens,
        "billed_total_tokens": tt.conv_total_tokens,
    }
