"""Sub-agent for delegated tasks.

Uses existing AgenticOrchestrator with isolated message context
and read-only tools to execute generic delegated tasks.
"""

from pathlib import Path

from core.chat_manager import ChatManager
from exceptions import LLMError
from llm.prompts import build_sub_agent_prompt
from utils.settings import sub_agent_settings


class HardLimitExceeded(Exception):
    """Raised when the sub-agent hits its hard token limit."""
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
        hard_limit_tokens=sub_agent_settings.hard_limit_tokens,
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

    def _chat_completion_with_token_hint(messages, **kwargs):
        """Prepend a system-level token budget hint (and soft-limit warning once) to every LLM call."""
        nonlocal _soft_limit_warned
        tt = temp_chat_manager.token_tracker
        hint = f"[Token budget: {tt.current_context_tokens:,} curr / {tt.conv_total_tokens:,} total]"

        if not _soft_limit_warned and tt.current_context_tokens >= sub_agent_settings.soft_limit_tokens:
            _soft_limit_warned = True
            hint = (
                f"WARNING: You have exceeded the soft token limit "
                f"({tt.current_context_tokens:,} / {sub_agent_settings.soft_limit_tokens:,}). "
                "STOP exploring and return your findings immediately. Do NOT call any more tools. "
                + hint
            )

        token_msg = {"role": "system", "content": hint}
        return original_chat_completion([token_msg, *messages], **kwargs)

    def _get_llm_response_with_hard_limit(allowed_tools=None):
        """Wrapper to check hard token limit and update panel with live token counts."""
        tt = temp_chat_manager.token_tracker

        # Check hard token limit before making LLM call
        # Use current_context_tokens (prompt size) not total_tokens (cumulative billing)
        # to catch prompt-length-over-limit errors before they hit the API.
        if tt.current_context_tokens >= sub_agent_settings.hard_limit_tokens:
            raise HardLimitExceeded(
                f"Sub-agent hard token limit exceeded: "
                f"{tt.current_context_tokens:,} / {sub_agent_settings.hard_limit_tokens:,} tokens."
            )

        # Update panel with live token counts
        # Order: conversation length (current context) first, total tokens billed second
        conv_length = tt.current_context_tokens
        total_billed = tt.conv_total_tokens
        if hasattr(panel_updater, 'token_info'):
            panel_updater.token_info = f"{conv_length:,} curr | {total_billed:,} total"
            panel_updater.append("")  # Refresh panel title

        return original_get_llm_response(allowed_tools=allowed_tools)

    # Apply both patches once, before the orchestrator loop starts
    orchestrator._get_llm_response = _get_llm_response_with_hard_limit
    temp_chat_manager.client.chat_completion = _chat_completion_with_token_hint

    hard_limit_exceeded = False

    try:
        # Run sub-agent task
        orchestrator.run(
            task_query,
            thinking_indicator=None,
            allowed_tools=sub_agent_settings.allowed_tools
        )
    except HardLimitExceeded:
        hard_limit_exceeded = True
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
        result = final_content

    usage = {
        "prompt_tokens": delta_prompt,
        "completion_tokens": delta_completion,
        "total_tokens": delta_total,
        "context_tokens": tt.current_context_tokens,
    }
    if delta_cost > 0:
        usage["cost"] = delta_cost

    return {
        "result": result,
        "usage": usage,
        "model": temp_chat_manager.client.model,
        "error": None,
        "hard_limit_exceeded": hard_limit_exceeded,
    }
