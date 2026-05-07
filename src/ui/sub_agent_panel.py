"""Toolbar-backed updater for sub-agent progress.

Replaces the former Rich Live panel with lightweight state updates
that write to the PTK toolbar via ProgressState.
"""

import logging
import threading

from core.tool_feedback import build_panel_tool_message

logger = logging.getLogger(__name__)

SPINNER_REFRESH_INTERVAL = 0.1


class SubAgentPanel:
    """Lightweight sub-agent progress updater backed by the PTK toolbar.

    Writes sub-agent state (tool calls, token counts, completion/error)
    to ``chat_manager.progress`` (a ProgressState instance) and triggers
    toolbar invalidation.  No Rich Live display, no terminal mode management.
    """

    # Tells _print_or_append() in tool_feedback.py that add_tool_call()
    # already recorded the compact user-visible message — skip the
    # plain handler summary to avoid duplicate output.
    handles_own_scrollback = True

    def __init__(self, query, chat_manager):
        self.chat_manager = chat_manager
        self.query = query
        self.total_tool_calls = 0
        self.tool_calls = []  # kept for backward-compat attribute access
        self._stop_timer = threading.Event()
        self._timer_thread = None
        self._timer_stopped = False
        self._subagent_finished = False

        # Capture prior generic spinner state so we can restore it later
        self._prev_spinner_active = chat_manager.progress.spinner_active
        self._prev_spinner_message = chat_manager.progress.spinner_message

        # Activate sub-agent state before clearing the generic spinner so the
        # main toolbar scheduler never observes a gap with no active progress.
        self.chat_manager.progress.start_subagent(query)
        self.chat_manager.progress.stop_spinner()
        self.chat_manager.invalidate_toolbar()

        # Start background refresh timer for toolbar spinner advancement
        self._timer_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._timer_thread.start()

    def _refresh_loop(self):
        """Periodically advance spinner and invalidate toolbar."""
        while not self._stop_timer.is_set():
            if self.chat_manager and hasattr(self.chat_manager, 'progress'):
                self.chat_manager.progress.advance_spinner()
                self.chat_manager.invalidate_toolbar()
            self._stop_timer.wait(SPINNER_REFRESH_INTERVAL)

    def _stop_refresh_timer(self):
        """Idempotently stop the background refresh timer thread."""
        if self._timer_stopped:
            return
        self._timer_stopped = True
        self._stop_timer.set()
        if self._timer_thread is not None:
            self._timer_thread.join(timeout=0.5)
            self._timer_thread = None

    def _restore_spinner(self):
        """Restore the generic spinner if it was active before the subagent."""
        if self._prev_spinner_active:
            self.chat_manager.progress.start_spinner(self._prev_spinner_message)

    # ------------------------------------------------------------------
    # Context manager protocol
    # ------------------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._stop_refresh_timer()
        # If the caller never called set_complete/set_error/cancel (or we're
        # exiting via exception), clean up subagent state and restore the
        # prior generic spinner so the toolbar doesn't get stuck.
        if not self._subagent_finished:
            progress = self.chat_manager.progress
            if progress.subagent_active or progress.subagent_done_state:
                progress.clear_subagent()
            self._restore_spinner()
            self.chat_manager.invalidate_toolbar()
        return False

    # ------------------------------------------------------------------
    # token_info property (backward compat)
    # ------------------------------------------------------------------

    @property
    def token_info(self):
        return self.chat_manager.progress.subagent_token_info

    @token_info.setter
    def token_info(self, value):
        self.chat_manager.progress.update_subagent_tokens(value)
        self.chat_manager.invalidate_toolbar()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_tool_call(self, tool_name, tool_result=None, command=None):
        """Add a tool call and update toolbar progress."""
        self.total_tool_calls += 1
        message = build_panel_tool_message(tool_name, tool_result, command)
        self.tool_calls.append(message)
        if len(self.tool_calls) > 5:
            self.tool_calls.pop(0)
        summary = tool_name
        if command:
            summary = f"{tool_name}: {command[:30]}"
        # Store the tool call line in the activity log
        self.chat_manager.progress.update_subagent_tool_call(summary, message or summary)
        self.chat_manager.invalidate_toolbar()

    def append(self, text):
        """Store result/detail text in the toolbar activity log.

        Called by _print_or_append after add_tool_call. Only store if the
        text looks like a result line (starts with ╰─ or similar), to avoid
        duplicating the tool name already stored by add_tool_call.
        """
        if text and text.strip():
            stripped = text.strip()
            # Skip if this is a duplicate of what add_tool_call already stored
            # (build_panel_tool_message includes the result line in its output)
            with self.chat_manager.progress._lock:
                log = self.chat_manager.progress.subagent_activity_log
                if log and stripped in log[-1]:
                    return
            self.chat_manager.progress.update_subagent_activity(stripped)
        self.chat_manager.invalidate_toolbar()

    def set_complete(self, usage=None):
        """Mark subagent as complete."""
        self._stop_refresh_timer()
        self._subagent_finished = True
        if usage and usage.get('total_tokens'):
            ctx_tokens = usage.get('context_tokens', 0)
            total_tokens = usage.get('total_tokens', 0)
            self.chat_manager.progress.update_subagent_tokens(
                f"{ctx_tokens:,} curr, {total_tokens:,} total"
            )
        self.chat_manager.progress.finish_subagent()
        self._restore_spinner()
        self.chat_manager.invalidate_toolbar()

    def set_error(self, message):
        """Mark subagent as error."""
        self._stop_refresh_timer()
        self._subagent_finished = True
        self.chat_manager.progress.finish_subagent(error=message)
        self._restore_spinner()
        self.chat_manager.invalidate_toolbar()

    def cancel(self):
        """Clear subagent display on user cancellation.

        Stops the refresh timer, clears subagent state from the progress
        bar, stops any active spinner, and invalidates the toolbar.
        Does NOT restore the generic spinner — user cancellation should
        fully clear active progress.
        """
        self._stop_refresh_timer()
        self._subagent_finished = True
        self.chat_manager.progress.clear_subagent()
        self.chat_manager.progress.stop_spinner()
        self.chat_manager.invalidate_toolbar()
