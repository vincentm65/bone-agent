"""Chat state and server lifecycle management."""

import json
import logging
import threading
import uuid

logger = logging.getLogger(__name__)

import queue
from typing import Optional, Any

from llm.client import LLMClient
from llm.config import get_providers, get_provider_display_name, get_provider_config, reload_config
from llm.prompts import build_system_prompt, build_swarm_admin_prompt
from core.skills import render_active_skills_section
from core.swarm_auto_turn import drain_inbox_to_prompts as _drain_inbox_to_prompts
from pathlib import Path
from llm.token_tracker import TokenTracker
from utils.settings import context_settings
from utils.logger import MarkdownConversationLogger
from utils.user_message_logger import UserMessageLogger
from utils.multimodal import content_text_for_logs
from utils.terminal_sanitize import SanitizedMessageList
from core.context_compaction import ContextCompaction
from core.queued_input import QueuedInput

class ChatManager:
    """Manages chat state, messages, and provider switching."""

    def __init__(self, compact_trigger_tokens: Optional[int] = None, provider: Optional[str] = None):
        # Initialize client with provider from global config (or override)
        self.client = LLMClient(provider=provider)
        self.conversation_id = str(uuid.uuid4())
        self.client.conversation_id = self.conversation_id
        self.messages = SanitizedMessageList()

        self.approve_mode = "safe"
        self.token_tracker = TokenTracker()
        self.context_token_estimate = 0
        self._context_dirty: bool = True  # Force initial token count
        self._context_tools_signature: str | None = None
        # In-session, memory-only task list (used in EDIT workflows)
        self.task_list = []
        self.task_list_title = None

        # Active toolbar interaction (rendered/dispatched by main prompt loop).
        # Formalised here so it's always present; toolbar_interactions.py
        # helpers use duck-typed access via this attribute.
        self._toolbar_interaction: Any = None

        # Pending toolbar interaction (staged for later resolution).
        # When set, the main prompt loop or input hook should present it to
        # the user and call resolve_pending_interaction() with the response.
        self._pending_interaction: Any = None

        # In-session active skill tracking. These skills are rendered into the
        # system prompt for the current chat.
        self.loaded_skills = set()

        # .gitignore filtering state
        self._gitignore_spec = None
        self._gitignore_mtime = None
        self._repo_root = None

        # Custom compaction threshold (overrides global context_settings if set)
        self._compact_trigger_tokens = compact_trigger_tokens

        # Swarm pool state (lazy-initialized, only present when in swarm admin mode)
        self.swarm_server: Any = None
        self.swarm_admin_mode: bool = False
        self.swarm_complete: bool = False
        self.swarm_status_page: int = 0  # 0=Workers, 1=Plan
        self.swarm_worker_scroll: int = 0  # Scroll offset for worker list toolbar
        # Maps swarm task_ids to task_list plan indices (populated by dispatch_swarm_task)
        self._swarm_task_plan_map: dict[str, int] = {}
        # Background poller drains server inbox items into this queue as
        # formatted auto-turn prompt strings.  The agentic orchestrator
        # checks this queue between LLM iterations so swarm events are
        # processed mid-turn instead of only at the prompt boundary.
        self._swarm_inject_queue: queue.Queue[str] = queue.Queue()
        self._swarm_inbox_poller_thread: threading.Thread | None = None
        self._swarm_inbox_poller_stop: threading.Event = threading.Event()

        # Subagent cancellation: thread-safe event set by the UI on Ctrl+C
        # and polled by run_sub_agent between iterations.
        self._subagent_cancel_event: threading.Event = threading.Event()
        # Agent turn cancellation: set by Ctrl+C while the background agent
        # thread is running. The orchestrator polls this between phases and
        # discards late LLM responses after in-flight HTTP calls complete.
        self._agent_cancel_event: threading.Event = threading.Event()

        # Queued user-message buffering and agent-running guard
        self._queued_input = QueuedInput(on_change=self.invalidate_toolbar)

        # Disable all compaction when True (used by sub-agents to preserve findings)
        self._compaction_disabled = False

        # Compaction engine (delegates to extracted module)
        self._context_compaction = ContextCompaction(self)

        # Transient progress state (spinner, subagent, active tool)
        from ui.status_state import ProgressState
        self.progress = ProgressState()

        # PTK toolbar invalidation callback (set by main.py)
        self._invalidate_toolbar = None  # callable that calls app.invalidate()

        # Conversation logging
        self.markdown_logger: Optional[MarkdownConversationLogger] = None
        if context_settings.log_conversations:
            self.markdown_logger = MarkdownConversationLogger(
                conversations_dir=context_settings.conversations_dir
            )

        # User message logging (always on, for dream memory system)
        self.user_message_logger = UserMessageLogger()

        # Compaction lock: prevents compaction during active tool execution
        # Set by agentic.py before executing tools, cleared after all results appended
        self._compaction_locked = False

        self._init_messages(reset_totals=True)

    def set_compaction_lock(self, locked):
        """Set or release the compaction lock.

        When locked, compaction is skipped entirely (no message removal,
        no summarization, no truncation). Used during tool execution to
        prevent orphaning tool_call_ids.
        """
        self._compaction_locked = locked

    # -- Subagent cancellation API ----------------------------------------

    def request_subagent_cancel(self) -> None:
        """Signal that the active subagent should stop as soon as possible."""
        self._subagent_cancel_event.set()

    def clear_subagent_cancel(self) -> None:
        """Clear the cancellation flag (call before starting a new subagent)."""
        self._subagent_cancel_event.clear()

    def get_subagent_cancel_event(self) -> threading.Event:
        """Return the raw threading.Event for wait() or select-style use."""
        return self._subagent_cancel_event

    def request_agent_cancel(self) -> None:
        """Signal that the active agent turn should stop as soon as possible."""
        self._agent_cancel_event.set()

    def clear_agent_cancel(self) -> None:
        """Clear the cancellation flag (call before starting a new agent turn).

        Replaces the internal event with a fresh one so that any
        orchestrator still holding a reference to the old (set) event
        continues to see the cancel signal.
        """
        self._agent_cancel_event = threading.Event()

    def is_agent_cancel_requested(self) -> bool:
        """Return True if cancellation has been requested for the agent turn."""
        return self._agent_cancel_event.is_set()

    def get_agent_cancel_event(self) -> threading.Event:
        """Return the raw threading.Event for wait() or select-style use."""
        return self._agent_cancel_event

    # ---------------------------------------------------------------------

    def _init_messages(self, reset_totals: bool = True, reset_costs: bool = False):
        """Initialize message history with system prompt and agents.md as initial exchange.

        Args:
            reset_totals: Reset cumulative token counts (default True).
            reset_costs: Reset cost accumulators (default False).
                         Set True on provider switch to clear stale billing state.
                         Kept False on /clear to preserve cumulative session costs.
        """
        # Start new conversation logging session
        if self.markdown_logger:
            self.markdown_logger.start_session()

        # Active skills are scoped to the current message history/session.
        self.loaded_skills = set()

        # Start with system prompt only
        self.messages = SanitizedMessageList([{"role": "system", "content": self._build_system_prompt()}])

        # Add agents.md as initial user/assistant exchange (only if it exists in cwd)
        # Skip for swarm admin mode — admin orchestrates workers, doesn't need codebase map
        if not getattr(self, 'swarm_admin_mode', False):
            user_msg, assistant_msg = self._load_agents_md()
            if user_msg and assistant_msg:
                self.messages.append({"role": "user", "content": user_msg})
                self.messages.append({"role": "assistant", "content": assistant_msg})

        # Log initial messages
        if self.markdown_logger:
            for msg in self.messages:
                self.markdown_logger.log_message(msg)

        # Reset session totals if requested (keep totals across /clear)
        # For a fresh conversation, cumulative totals start at 0 (no API calls made yet)
        if reset_totals:
            if reset_costs:
                self.token_tracker.reset_all()
            else:
                self.token_tracker.reset(prompt_tokens=0, completion_tokens=0)

        # Always reset conversation tokens (resets on /new and fresh starts)
        self.token_tracker.reset_conversation()

        # Initialize context tokens with actual message count (including tools if enabled)
        self._update_context_tokens(force=True)
        self.context_token_estimate = self.token_tracker.current_context_tokens

    def _build_system_prompt(self) -> str:
        """Build system prompt."""
        active_skills_section = render_active_skills_section(self.loaded_skills)
        if self.swarm_admin_mode:
            return build_swarm_admin_prompt(active_skills_section=active_skills_section)
        return build_system_prompt(active_skills_section=active_skills_section)

    def update_system_prompt(self):
        """Rebuild system prompt in-place (e.g. after hotswap or session reset)."""
        if not self.messages:
            raise RuntimeError("Cannot update system prompt: messages array is empty")

        if self.messages[0]["role"] != "system":
            raise RuntimeError(f"Cannot update system prompt: messages[0] has role '{self.messages[0]['role']}', expected 'system'")

        self.messages[0]["content"] = self._build_system_prompt()
        self._update_context_tokens(force=True)

    def _load_agents_md(self) -> tuple[str, str]:
        """Load agents.md content and prepare user/assistant exchange.

        Returns:
            tuple: (user_message, assistant_message)
        """
        # Check for agents.md in current working directory (user's project)
        agents_path = Path.cwd() / "agents.md"

        if agents_path.exists():
            map_content = agents_path.read_text(encoding="utf-8").strip()
            user_msg = (
                "Here is the codebase map for this project. "
                "This provides an overview of the repository structure and file purposes. "
                "Use this as a reference when exploring the codebase.\n\n"
                f"## Codebase Map (auto-generated from agents.md)\n\n{map_content}"
            )
            assistant_msg = (
                "I've received the codebase map. I'll use this as a reference when "
                "exploring the repository, but I'll always verify current state by "
                "reading files and searching the codebase before making changes."
            )
        else:
            # No codebase map available - skip entirely
            user_msg = ""
            assistant_msg = ""

        return user_msg, assistant_msg

    def _update_context_tokens(self, tools=None, force=False):
        """Recount and update context token estimate. Skips if not dirty.

        Args:
            tools: Override auto-detection of tools.
            force: Force recount even if not dirty (used by compaction, init, etc.).
        """
        tools_signature = json.dumps(tools, sort_keys=True, default=str) if tools is not None else None
        if (
            not force
            and not self._context_dirty
            and tools_signature == self._context_tools_signature
        ):
            return
        self._context_dirty = False
        message_tokens = self._count_tokens(self.messages)

        # Count tool tokens if tools are provided or enabled
        if tools is None:
            from llm.config import TOOLS_ENABLED
            if not TOOLS_ENABLED:
                self.token_tracker.set_context_tokens(message_tokens)
                self.context_token_estimate = message_tokens
                self._context_tools_signature = None
                return
            else:
                from tools import TOOLS
                tools = TOOLS()
                tools_signature = json.dumps(tools, sort_keys=True, default=str)

        if tools:
            # Serialize tools to JSON (the API payload form) and use the shared
            # provider-aware estimator.  This is accurate for all providers
            # (tiktoken is used when available, including as an approximation for
            # Anthropic, with a conservative byte-aware fallback).
            tools_json = json.dumps(tools)
            tool_tokens = ContextCompaction._estimate_tokens_for_text(tools_json)

            total_tokens = message_tokens + tool_tokens
        else:
            total_tokens = message_tokens

        self.token_tracker.set_context_tokens(total_tokens)
        self.context_token_estimate = total_tokens
        self._context_tools_signature = tools_signature

    # -- Public compaction API (delegate to ContextCompaction) -----------------

    _serialize_message_payload = staticmethod(ContextCompaction._serialize_message_payload)
    _estimate_tokens_for_text = staticmethod(ContextCompaction._estimate_tokens_for_text)
    _estimate_tokens_for_payload = staticmethod(ContextCompaction._estimate_tokens_for_text)

    def _count_tokens(self, messages):
        """Compatibility wrapper for the extracted token counter."""
        return self._get_context_compaction()._count_tokens(messages)

    def _estimate_message_tokens(self, msg):
        """Compatibility wrapper for the extracted per-message estimator."""
        return self._get_context_compaction()._estimate_message_tokens(msg)

    def _get_context_compaction(self):
        """Return the compaction engine, creating it for __new__ test doubles."""
        if not hasattr(self, "_context_compaction"):
            self._context_compaction = ContextCompaction(self)
        return self._context_compaction

    def compact_tool_results(self, skip_token_update=False,
                              uncompacted_tail_tokens=None, min_tool_blocks=None):
        """Replace completed tool-result blocks with summaries."""
        return self._get_context_compaction().compact_tool_results(
            skip_token_update=skip_token_update,
            uncompacted_tail_tokens=uncompacted_tail_tokens,
            min_tool_blocks=min_tool_blocks,
        )

    def compact_history(self, console=None, trigger="manual"):
        """Compact chat history while preserving recent context."""
        return self._get_context_compaction().compact_history(console=console, trigger=trigger)

    def maybe_auto_compact(self, console=None):
        """Check token count and auto-compact if over threshold."""
        return self._get_context_compaction().maybe_auto_compact(console)

    def ensure_context_fits(self, console=None):
        """Ensure context fits within hard_limit_tokens before sending to LLM."""
        return self._get_context_compaction().ensure_context_fits(console)

    def get_gitignore_spec(self, repo_root: Path):
        """Get cached or load PathSpec object for .gitignore filtering.

        Caches the spec and reloads if .gitignore is modified.

        Args:
            repo_root: Repository root directory

        Returns:
            pathspec.PathSpec or None if .gitignore doesn't exist
        """
        gitignore_path = repo_root / ".gitignore"

        # Check if we need to reload
        current_mtime = None
        if gitignore_path.exists():
            current_mtime = gitignore_path.stat().st_mtime

        # Reload if: (1) not initialized, (2) repo changed, (3) file modified
        if (
            self._gitignore_spec is None
            or self._repo_root != repo_root
            or current_mtime != self._gitignore_mtime
        ):
            from utils.gitignore_filter import load_gitignore_spec

            self._repo_root = repo_root
            self._gitignore_mtime = current_mtime
            self._gitignore_spec = load_gitignore_spec(repo_root)

        return self._gitignore_spec

    def switch_provider(self, provider_name):
        """Switch LLM provider.

        Args:
            provider_name: Provider name ('local' or 'openrouter')

        Returns:
            str: Result message
        """
        providers = get_providers()
        if provider_name not in providers:
            available = ', '.join(get_provider_display_name(provider) for provider in providers)
            return f"Invalid provider. Use /provider to list. Available: {available}"

        previous_provider = self.client.provider

        if self.client.switch_provider(provider_name):
            self.token_tracker.reset_all()
            self.token_tracker.reset_conversation()
            self._update_context_tokens(force=True)
            self.context_token_estimate = self.token_tracker.current_context_tokens
            if self.markdown_logger:
                self.markdown_logger.start_session()
            provider_label = get_provider_display_name(provider_name)
            return f"Switched to {provider_label} provider."
        return "Provider switch failed."

    def reload_config(self):
        """Reload configuration from disk and update client.

        This should be called after any config change (provider, model, api key).
        """
        reload_config()
        self.client.sync_provider_from_config()

    # ===== Config Methods (for agent use) =====

    def set_provider(self, provider_name: str) -> str:
        """Set provider for current session (agent-accessible).

        Args:
            provider_name: Provider name to switch to.

        Returns:
            str: Result message.
        """
        return self.switch_provider(provider_name)

    def cycle_approve_mode(self, mode: str | None = None) -> str:
        """Cycle to next approval mode, or set to a specific mode.

        Args:
            mode: If provided, set this mode directly. Otherwise cycle.

        Returns:
            str: The new approval mode.
        """
        from llm.config import CYCLEABLE_APPROVE_MODES
        modes = CYCLEABLE_APPROVE_MODES
        if mode is not None:
            self.approve_mode = mode
        else:
            try:
                next_index = (modes.index(self.approve_mode) + 1) % len(modes)
            except ValueError:
                next_index = 0
            self.approve_mode = modes[next_index]
        return self.approve_mode

    def reset_session(self):
        """Reset chat session (clear messages and task list).

        This is a public wrapper for _init_messages that also clears
        the in-session task list.
        """
        # End current conversation logging session before reset
        if self.markdown_logger:
            self.markdown_logger.end_session()

        self._init_messages(reset_totals=False)
        self.task_list.clear()
        self.task_list_title = None

    def log_message(self, message: dict):
        """Log a message to the conversation logger.

        Args:
            message: Message dict to log
        """
        if self.markdown_logger:
            self.markdown_logger.log_message(message)

        # Log user messages to JSONL for dream memory processing (only if memory enabled)
        if message.get("role") == "user" and message.get("content") and self.user_message_logger:
            from llm.config import MEMORY_SETTINGS
            if MEMORY_SETTINGS.get("enabled", True):
                self.user_message_logger.log_user_message(
                    content_text_for_logs(message["content"]),
                    project_dir=Path.cwd().resolve(),
                )

    # ===== Centralized message mutation APIs =====

    def add_message(self, message: dict, log: bool = True) -> None:
        """Append a single message, optionally logging it.
        
        Args:
            message: Message dict to append (will be sanitized by SanitizedMessageList).
            log: If True, log the message to the conversation logger.
        """
        self.messages.append(message)
        if log:
            self.log_message(message)
        self._context_dirty = True

    def extend_messages(self, messages: list[dict], log: bool = True) -> None:
        """Extend messages with a list, optionally logging each.
        
        Args:
            messages: List of message dicts to append.
            log: If True, log each message.
        """
        self.messages.extend(messages)
        if log:
            for msg in messages:
                self.log_message(msg)
        self._context_dirty = True

    def replace_messages(self, messages: list[dict], sync_log: bool = True) -> None:
        """Replace the entire message list with new messages.
        
        Used by compaction methods that rebuild the message list.
        
        Args:
            messages: New message list (will be wrapped in SanitizedMessageList).
            sync_log: If True, rewrite the conversation log to match.
        """
        self.messages = SanitizedMessageList(messages)
        if sync_log:
            self.sync_log()
        self._context_dirty = True

    def pop_message(self, index: int, sync_log: bool = False) -> dict:
        """Remove and return a message by index.
        
        Args:
            index: Index of the message to remove.
            sync_log: If True, rewrite the log after removal.
        
        Returns:
            The removed message dict.
        """
        msg = self.messages.pop(index)
        if sync_log:
            self.sync_log()
        self._context_dirty = True
        return msg

    def mark_context_dirty(self) -> None:
        """Mark the context token estimate as stale, forcing a recount on next access."""
        self._context_dirty = True

    def invalidate_toolbar(self) -> None:
        """Trigger a toolbar redraw if a PTK app is active. Thread-safe."""
        if self._invalidate_toolbar:
            try:
                self._invalidate_toolbar()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Pending toolbar interaction contract
    # ------------------------------------------------------------------

    def set_pending_interaction(self, interaction: Any) -> None:
        """Stage a PendingInteraction for the main prompt loop to resolve."""
        self._pending_interaction = interaction

    def get_pending_interaction(self) -> Any:
        """Return the current pending interaction, or None."""
        return self._pending_interaction

    def clear_pending_interaction(self) -> None:
        """Remove the current pending interaction without resolving it."""
        self._pending_interaction = None

    def resolve_pending_interaction(self, result: Any = None) -> bool:
        """Resolve the current pending interaction with a result.

        If a pending interaction exists and hasn't already been resolved,
        calls ``interaction.resolve(result)`` and clears it.  Safe to
        call when no pending interaction is set (returns False).

        Args:
            result: The value to pass through to the pending
                    interaction's ``resolve()`` method.

        Returns:
            True if a pending interaction was resolved, False if there
            was nothing to resolve or it was already resolved.
        """
        interaction = self._pending_interaction
        if interaction is None:
            return False
        if interaction.is_resolved:
            # Already resolved externally; just clear our reference.
            self._pending_interaction = None
            return False
        interaction.resolve(result)
        self._pending_interaction = None
        return True

    def start_swarm_inbox_poller(self, poll_interval: float = 0.1) -> None:
        """Start a background daemon thread that drains the swarm server
        inbox and queues formatted auto-turn prompts for the agentic loop.

        The poller never touches ``self.messages`` or the LLM client — it
        only drains ``server._inbox`` and pushes strings into
        ``self._swarm_inject_queue``.  The agentic orchestrator is
        responsible for picking up those strings and injecting them as
        user messages between LLM iterations.

        Safe to call multiple times — resets stop signal and spawns a
        new thread if the previous one has already exited.

        Args:
            poll_interval: Seconds between inbox polls (default 100ms).
        """
        # If a thread is alive, it's already running — don't spawn a duplicate.
        if self._swarm_inbox_poller_thread is not None and self._swarm_inbox_poller_thread.is_alive():
            return

        # Reset the stop event so the new poll loop runs until explicitly stopped.
        self._swarm_inbox_poller_stop.clear()

        def _poll_loop() -> None:
            import time as _time
            while not self._swarm_inbox_poller_stop.is_set():
                try:
                    server = self.swarm_server
                    if server is not None:
                        # Fast path: drain_prompts returns quickly if empty
                        prompts = _drain_inbox_to_prompts(server)
                        for prompt in prompts:
                            self._swarm_inject_queue.put(prompt)
                except Exception as e:
                    logger.error("inbox poller error: %s", e, exc_info=True)
                # Respect stop signal even during sleep — use a short sleep
                # so we don't block the shutdown signal.
                self._swarm_inbox_poller_stop.wait(timeout=poll_interval)

        self._swarm_inbox_poller_thread = threading.Thread(
            target=_poll_loop, daemon=True, name="swarm-inbox-poller"
        )
        self._swarm_inbox_poller_thread.start()

    def stop_swarm_inbox_poller(self) -> None:
        """Stop the inbox poller thread and flush stale prompts. Idempotent."""
        self._swarm_inbox_poller_stop.set()

        thread = self._swarm_inbox_poller_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
            if thread.is_alive():
                logger.warning(
                    "swarm inbox poller thread did not exit within timeout — "
                    "keeping reference to prevent double-poller"
                )
                return
        self._swarm_inbox_poller_thread = None

        # Flush stale swarm prompts so a future swarm doesn't process old events.
        self._drain_queue(self._swarm_inject_queue)

    def _reset_swarm_state(self) -> None:
        """Clear all swarm-related state. Called on /swarm close."""
        self.swarm_complete = False
        self._swarm_task_plan_map.clear()
        self.task_list = None
        self.task_list_title = None

    def has_pending_swarm_work(self) -> bool:
        """Check whether any swarm work is waiting — either in the server
        inbox or already drained into the inject queue by the poller.

        Use this instead of ``server.has_pending()`` in inputhooks and
        fallback checks so items that the poller already moved into the
        inject queue still trigger the auto-turn path.
        """
        server = self.swarm_server
        if server is not None and server.has_pending():
            return True
        return not self._swarm_inject_queue.empty()

    @staticmethod
    def _drain_queue(q: queue.Queue, limit: int | None = None) -> list:
        """Drain up to *limit* items from *q* into a list (0 on empty)."""
        items = []
        while True:
            if limit is not None and len(items) >= limit:
                break
            try:
                items.append(q.get_nowait())
            except Exception:
                break
        return items

    def drain_inject_queue(self) -> list[str]:
        """Drain all formatted auto-turn prompts from the inject queue."""
        return self._drain_queue(self._swarm_inject_queue)

    # -- Agent-running guard and queued user messages ---------------------

    def set_agent_running(self, running: bool) -> None:
        """Set or clear the agent-running flag and invalidate toolbar."""
        self._queued_input.set_agent_running(running)
        self.invalidate_toolbar()

    def is_agent_running(self) -> bool:
        """Return True if an agent turn is currently in progress."""
        return self._queued_input.is_agent_running()

    def enqueue_user_message(self, content: Any) -> bool:
        """Buffer a user message for the next turn. Rejects empty/whitespace strings."""
        return self._queued_input.enqueue(content)

    def queued_user_message_count(self) -> int:
        """Return the number of queued user messages (thread-safe)."""
        return self._queued_input.count()

    def has_queued_user_messages(self) -> bool:
        """Return True if there are queued user messages waiting."""
        return self._queued_input.has_items()

    def drain_queued_user_messages(self, limit: int | None = None) -> list[Any]:
        """Drain queued user messages in FIFO order."""
        return self._queued_input.drain(limit)

    def clear_queued_user_messages(self) -> int:
        """Remove all queued user messages and return the count removed."""
        return self._queued_input.clear()

    def sync_log(self):
        """Rewrite the entire conversation log to match current message state.

        This should be called after any operation that modifies the messages array:
        - After adding new messages
        - After compaction
        - After mode changes (which modify system prompts)
        """
        if self.markdown_logger:
            self.markdown_logger.rewrite_log(self.messages)

    def end_conversation(self):
        """End the current conversation logging session."""
        if self.markdown_logger:
            self.markdown_logger.end_session()

    def set_logging(self, enabled: bool) -> bool:
        """Enable or disable conversation logging. Returns the new state."""
        from utils.logger import MarkdownConversationLogger

        current_state = self.markdown_logger is not None
        if enabled == current_state:
            return current_state

        if enabled:
            self.markdown_logger = MarkdownConversationLogger(
                conversations_dir=context_settings.conversations_dir
            )
            self.markdown_logger.start_session()
            for msg in self.messages:
                self.markdown_logger.log_message(msg)
        else:
            self.markdown_logger.end_session()
            self.markdown_logger = None

        return enabled
