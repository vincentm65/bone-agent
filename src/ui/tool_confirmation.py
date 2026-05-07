"""Interactive tool confirmation panel — toolbar-hosted.

Replaced the old inline-nested-Application approach with a ToolbarInteraction
subclass rendered via ``run_toolbar_interaction()``.  The public
``ToolConfirmationPanel`` API is unchanged.
"""

from typing import Optional, Tuple

from ui.toolbar_interactions import (
    ToolbarInteraction,
    run_toolbar_interaction,
    escape_html,
    styled,
    make_section,
)

# Re-export for callers that used the original import path.
__all__ = ["ToolConfirmationPanel"]




# ---------------------------------------------------------------------------
# Toolbar-hosted interaction (internal)
# ---------------------------------------------------------------------------

class _ToolApprovalInteraction(ToolbarInteraction):
    """Compact toolbar tool-approval interaction.

    Renders a one-line or few-line toolbar with arrow-key navigation,
    advice text entry, and Accept All Edits support.  Completion is
    signalled via ``finish((action, guidance))``.
    """

    _CURSOR = "> "

    def __init__(
        self,
        tool_command: str,
        reason: Optional[str],
        options: list[dict],
        cycle_approve_mode=None,
    ) -> None:
        super().__init__()
        self._tool_command = tool_command
        self._reason = reason
        self._options = options
        self._cycle_approve_mode = cycle_approve_mode
        self._selected_index = 0
        self._advice_buffer = ""
        # Modes: "navigate" (choose option) | "advise" (type advice)
        self._mode = "navigate"

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    @staticmethod
    def _append_field(
        lines: list[str], label: str, value: object, *, formatted: bool = False
    ) -> None:
        value_text = str(value)
        if not formatted:
            value_text = escape_html(value_text)
        value_lines = value_text.splitlines() or [""]
        lines.append(f"<b>{escape_html(label)}:</b> {value_lines[0]}")
        for continuation in value_lines[1:]:
            lines.append(f"    {continuation}")

    def _render_navigate(self) -> str:
        """Render the option-navigation toolbar content."""
        lines: list[str] = []

        # Tool name (truncated for compactness)
        cmd_display = self._tool_command
        if len(cmd_display) > 72:
            cmd_display = cmd_display[:69] + "..."
        lines.append(styled(escape_html(cmd_display), fg="#cad2d9", bold=True))

        # Reason (single line, truncated)
        if self._reason:
            reason_display = self._reason.splitlines()[0]
            if len(reason_display) > 90:
                reason_display = reason_display[:87] + "..."
            lines.append(styled(escape_html(reason_display), fg="#888888"))

        lines.append("")  # spacer

        # Options row
        option_texts: list[str] = []
        for idx, opt in enumerate(self._options):
            text = opt.get("text", "")
            if idx == self._selected_index:
                option_texts.append(
                    styled(f"{self._CURSOR}{text}", fg="#FFFFFF", bold=True)
                )
            else:
                option_texts.append(styled(f"  {text}", fg="#6a737d"))
        lines.append("   ".join(option_texts))

        return make_section(lines=lines)

    def _render_advise(self) -> str:
        """Render advice-text input mode."""
        lines: list[str] = []
        lines.append(
            styled("Enter advice (\u21b5 confirm, Esc back):", fg="#cad2d9", bold=True)
        )
        buf = self._advice_buffer
        cursor = styled("\u258c", fg="#FFFFFF")  # full block cursor
        if buf:
            lines.append(styled(escape_html(buf) + cursor, fg="#FFFFFF"))
        else:
            lines.append(cursor)
        return make_section(lines=lines)

    def render(self) -> str:
        if self._mode == "advise":
            return self._render_advise()
        return self._render_navigate()

    # ------------------------------------------------------------------
    # Key handling
    # ------------------------------------------------------------------

    def _safe_invalidate(self, event: object) -> None:
        """Invalidate the prompt_toolkit app if available."""
        try:
            event.app.invalidate()  # type: ignore[union-attr]
        except Exception:
            pass

    def _handle_navigate_key(self, event: object) -> bool:
        """Handle key events in option-navigation mode."""
        name = self._key_name(event)

        if name in ("up", "left"):
            if self._selected_index > 0:
                self._selected_index -= 1
                self._safe_invalidate(event)
            return True

        if name in ("down", "right"):
            if self._selected_index < len(self._options) - 1:
                self._selected_index += 1
                self._safe_invalidate(event)
            return True

        if name == "enter":
            selected_value = self._options[self._selected_index].get("value")
            if selected_value == "accept_all_edits":
                if self._cycle_approve_mode:
                    self._cycle_approve_mode("accept_edits")
                self.finish(("accept", None))
            elif selected_value == "advise":
                self._mode = "advise"
                self._advice_buffer = ""
                self._safe_invalidate(event)
            else:
                self.finish((selected_value, None))
            return True

        if name == "escape":
            self.finish(("cancel", None))
            return True

        # Quick-select by first letter of each option (case-insensitive).
        data = self._key_data(event)
        if data and len(data) == 1:
            letter = data.lower()
            for idx, opt in enumerate(self._options):
                opt_text = opt.get("text", "")
                if opt_text.lower().startswith(letter):
                    self._selected_index = idx
                    self._safe_invalidate(event)
                    break
            return True

        return True  # consume anything else too while navigating

    def _handle_advise_key(self, event: object) -> bool:
        """Handle key events in advice-text input mode."""
        name = self._key_name(event)

        if name == "escape":
            self._mode = "navigate"
            self._advice_buffer = ""
            self._safe_invalidate(event)
            return True

        if name == "enter":
            advice = self._advice_buffer.strip()
            if not advice:
                self.finish(("cancel", None))
            else:
                self.finish(("advise", advice))
            return True

        if name == "backspace":
            self._advice_buffer = self._advice_buffer[:-1]
            self._safe_invalidate(event)
            return True

        # Printable characters
        data = self._key_data(event)
        if data and len(data) == 1:
            self._advice_buffer += data
            self._safe_invalidate(event)
            return True

        return True

    def handle_key(self, event: object) -> bool:
        if self._mode == "advise":
            return self._handle_advise_key(event)
        return self._handle_navigate_key(event)

    # ------------------------------------------------------------------
    # Key extraction helpers (duck-type prompt_toolkit events)
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_key_name(name: str) -> str:
        """Normalize key names with platform-dependent variants.

        Some terminals send Control-M for Enter, Control-H for Backspace,
        etc.  This maps those variants to the canonical names used by
        the navigate/advise key handlers.
        """
        name = name.lower()
        if name in ("c-m", "\r", "\n"):
            return "enter"
        if name in ("c-h",):
            return "backspace"
        if name in ("c-i", "\t"):
            return "tab"
        if name in ("space",):
            return " "
        if name in ("delete", "c-d"):
            return "delete"
        return name

    @staticmethod
    def _key_name(event: object) -> Optional[str]:
        """Extract a normalized key name from a prompt_toolkit KeyPressEvent.

        Inspects the last key press in the sequence (``key_sequence[-1]``).
        Prefers ``Keys`` enum ``.name``, falling back to ``.data`` for
        printable characters when no enum name is available.
        """
        try:
            seq = event.key_sequence  # type: ignore[union-attr]
            if seq:
                press = seq[-1]
                # Prefer the Keys enum name if present.
                key = getattr(press, "key", None)
                name = getattr(key, "name", None) if key is not None else None
                if not name:
                    # Fall back to printable data on the KeyPress itself.
                    name = getattr(press, "data", None)
                return _ToolApprovalInteraction._normalize_key_name(name) if name else None
        except Exception:
            pass
        return None

    @staticmethod
    def _key_data(event: object) -> Optional[str]:
        """Extract printable character data from a prompt_toolkit KeyPressEvent.

        ``.data`` lives on the ``KeyPress`` object (not on ``.key``).
        """
        try:
            seq = event.key_sequence  # type: ignore[union-attr]
            if seq:
                press = seq[-1]
                data = getattr(press, "data", None)
                return data if data else None
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Public compatibility wrapper
# ---------------------------------------------------------------------------

class ToolConfirmationPanel:
    """Interactive panel for tool execution confirmation — toolbar-hosted.

    Public API is unchanged: construct with tool info, call ``run()``,
    receive ``(action, guidance)``.  Internally delegates to a
    ``_ToolApprovalInteraction`` run via ``run_toolbar_interaction()``.
    """

    # Public constants (kept for backward compatibility)
    SUMMARY_DISPLAY_DELAY = 0.5  # Retained for API compat; no longer used
    CURSOR = "> "
    STANDARD_OPTIONS = [
        {"value": "accept", "text": "Accept"},
        {"value": "advise", "text": "Advise"},
        {"value": "cancel", "text": "Cancel"},
    ]
    EDIT_OPTIONS = [
        {"value": "accept", "text": "Accept"},
        {"value": "accept_all_edits", "text": "Accept All Edits"},
        {"value": "advise", "text": "Advise"},
        {"value": "cancel", "text": "Cancel"},
    ]

    def __init__(
        self,
        tool_command: str,
        reason: Optional[str] = None,
        is_edit_tool: bool = False,
        cycle_approve_mode=None,
        chat_manager=None,
    ) -> None:
        self.tool_command = tool_command
        self.reason = reason
        self.is_edit_tool = is_edit_tool
        self.cycle_approve_mode = cycle_approve_mode
        self._chat_manager = chat_manager
        self._options = self.EDIT_OPTIONS if is_edit_tool else self.STANDARD_OPTIONS

    def run(self) -> Tuple[str, Optional[str]]:
        """Display the confirmation interaction and wait for user input.

        Returns:
            Tuple of (action, guidance_text):
                - action: "accept", "advise", or "cancel"
                - guidance_text: User's advice if action is "advise", None otherwise
        """
        interaction = _ToolApprovalInteraction(
            tool_command=self.tool_command,
            reason=self.reason,
            options=list(self._options),  # defensive copy
            cycle_approve_mode=self.cycle_approve_mode,
        )
        result = run_toolbar_interaction(
            interaction, chat_manager=self._chat_manager
        )
        # result is None when cancelled / interrupted / timed out
        if result is None:
            return ("cancel", None)
        return result
