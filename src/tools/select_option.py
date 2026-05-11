"""Interactive selection tool for presenting multiple-choice questions to the user."""

import shutil
import textwrap
from html import escape as _html_escape
from typing import Optional, List, Dict, Any

from prompt_toolkit import HTML

from ui.toolbar_interactions import ToolbarInteraction, run_toolbar_interaction

from .helpers.base import tool

# Sentinel value used to detect when user selects the custom input option
CUSTOM_INPUT_SENTINEL = "__custom_input__"
CUSTOM_INPUT_OPTION = {
    "value": CUSTOM_INPUT_SENTINEL,
    "text": "Type your own input..."
}


class SelectionPanel(ToolbarInteraction):
    """Toolbar-hosted selection panel with arrow key navigation and inline custom input."""

    # Cursor indicator
    _CURSOR = "> "

    def __init__(self, questions: List[Dict[str, Any]], chat_manager=None):
        """Initialize the selection panel.

        Args:
            questions: List of question dicts with 'question', 'options'
                       (each with 'value', 'text', optional 'description').
            chat_manager: Optional ChatManager for admin interrupt polling.
        """
        super().__init__()
        self.questions = questions
        self._chat_manager = chat_manager

        # Initialize for multi-question mode (handles both single and multiple questions)
        self.current_question_idx = 0
        self.selections = [None] * len(questions)
        # Initialize selected_index for each question
        self.selected_indices = [0] * len(questions)

        # Inline custom input editing state
        self._editing_custom_input = False
        self._custom_input_texts: Dict[int, str] = {}  # question_idx -> typed text

        # Multi-select state: per-question set of checked option indices
        self._checked_indices: Dict[int, set] = {
            q_idx: set() for q_idx in range(len(questions))
        }

    def _is_multi_select(self, q_idx: int = None) -> bool:
        """Check if a question is in multi-select mode.

        Args:
            q_idx: Question index, defaults to current_question_idx
        """
        if q_idx is None:
            q_idx = self.current_question_idx
        return bool(self.questions[q_idx].get("multi_select", False))

    def _is_custom_input_selected(self) -> bool:
        """Check if the custom input option is currently selected."""
        q_idx = self.current_question_idx
        options = self.questions[q_idx].get("options", [])
        opt_idx = self.selected_indices[q_idx]
        if opt_idx < len(options):
            return options[opt_idx].get("value") == CUSTOM_INPUT_SENTINEL
        return False

    def _selected_multi_values(self, q_idx: int = None) -> list:
        """Return selected values for a multi-select question.

        Includes checked option values (in option order) plus any custom
        input text.  Returns an empty list when nothing is selected.
        """
        if q_idx is None:
            q_idx = self.current_question_idx
        options = self.questions[q_idx].get("options", [])
        checked = self._checked_indices.get(q_idx, set())
        values = []
        for i in sorted(checked):
            if i < len(options) and options[i].get("value") != CUSTOM_INPUT_SENTINEL:
                values.append(options[i].get("value"))
        # Custom input is confirmed by pressing Enter while editing the
        # sentinel option. The sentinel itself is intentionally not checkable,
        # so include non-empty custom text directly instead of requiring a
        # checked sentinel index.
        custom_text = self._custom_input_texts.get(q_idx, "").strip()
        if custom_text:
            values.append(custom_text)
        return values

    # ------------------------------------------------------------------
    # ToolbarInteraction interface
    # ------------------------------------------------------------------

    def render(self) -> str:
        """Return compact HTML for bottom-toolbar rendering.

        Shows question header with progress, windowed options around
        the focused item, and a short controls hint.  Descriptions are
        omitted to keep the toolbar compact.
        """
        lines = []
        q_idx = self.current_question_idx
        question = self.questions[q_idx]
        q_num = q_idx + 1
        q_total = len(self.questions)
        is_single = q_total == 1

        q_text = _html_escape(question.get("question", ""))

        # Controls hint
        if self._editing_custom_input:
            hint = "Type \u00b7 Enter confirm \u00b7 Esc back"
        elif self._is_multi_select(q_idx):
            hint = "\u2191\u2193 nav \u00b7 Space toggle \u00b7 Enter confirm \u00b7 Esc cancel"
        elif is_single:
            hint = "\u2191\u2193 nav \u00b7 Enter select \u00b7 Esc cancel"
        else:
            hint = "\u2191\u2193 options \u00b7 \u2190\u2192 questions \u00b7 Enter select \u00b7 Esc cancel"

        if is_single:
            lines.append(f"<b>{q_text}</b>    <style fg='#888888'>{hint}</style>")
        else:
            lines.append(f"<b>Q {q_num}/{q_total}: {q_text}</b>    <style fg='#888888'>{hint}</style>")

        # Options — windowed when the list is long
        options = question.get("options", [])
        _MAX_VISIBLE = 7
        total_opts = len(options)
        focused = self.selected_indices[q_idx]

        if total_opts <= _MAX_VISIBLE:
            visible_start = 0
            visible_end = total_opts
        else:
            half = _MAX_VISIBLE // 2
            visible_start = max(0, focused - half)
            visible_end = min(total_opts, visible_start + _MAX_VISIBLE)
            # Clamp to the right edge so we always show _MAX_VISIBLE items
            if visible_end - visible_start < _MAX_VISIBLE:
                visible_start = max(0, visible_end - _MAX_VISIBLE)

        if visible_start > 0:
            lines.append(
                f'<style fg="#888888">  \u22ef {visible_start} more above \u22ef</style>'
            )

        for o_idx in range(visible_start, visible_end):
            opt = options[o_idx]
            self._render_option(opt, o_idx, q_idx, o_idx == focused, lines)

        if visible_end < total_opts:
            lines.append(
                f'<style fg="#888888">  \u22ef {total_opts - visible_end} more below \u22ef</style>'
            )

        return "\n".join(lines)

    def _render_option(self, opt, o_idx, q_idx, is_focused, lines):
        """Render a single option line for toolbar display (no descriptions)."""
        text = _html_escape(opt.get("text", ""))
        is_custom = opt.get("value") == CUSTOM_INPUT_SENTINEL
        multi = self._is_multi_select(q_idx)
        checked = o_idx in self._checked_indices.get(q_idx, set())

        if is_focused:
            if is_custom and self._editing_custom_input:
                typed = _html_escape(self._custom_input_texts.get(q_idx, ""))
                display = typed if typed else text
                self._render_wrapped(lines, display, self._CURSOR, "white", bold=True)
            elif multi and not is_custom:
                marker = "\u25c9" if checked else "\u25cb"
                lines.append(
                    f'<style fg="white" bold="true">{self._CURSOR}{marker} {text}</style>'
                )
            else:
                lines.append(
                    f'<style fg="white" bold="true">{self._CURSOR}{text}</style>'
                )
        else:
            if multi and not is_custom:
                marker = "\u25c9" if checked else "\u25cb"
                if checked:
                    lines.append(
                        f'<style fg="#5F9EA0">  {marker} {text}</style>'
                    )
                else:
                    lines.append(
                        f'<style fg="gray">  {marker} {text}</style>'
                    )
            else:
                if is_custom:
                    typed = _html_escape(self._custom_input_texts.get(q_idx, ""))
                    display = typed if typed else text
                    self._render_wrapped(lines, display, "  ", "gray")
                else:
                    display = text
                    lines.append(f'<style fg="gray">  {display}</style>')

    @staticmethod
    def _toolbar_width() -> int:
        """Return a conservative visible width for toolbar lines."""
        try:
            return max(20, shutil.get_terminal_size(fallback=(80, 24)).columns - 1)
        except Exception:
            return 79

    def _render_wrapped(self, lines, display, prefix, color, *, bold=False):
        """Render a long string wrapped to the toolbar width."""
        width = self._toolbar_width()
        prefix_len = len(prefix)
        available = max(20, width - prefix_len)

        if len(display) <= available:
            tag = f'fg="{color}"'
            if bold:
                tag += ' bold="true"'
            lines.append(f'<style {tag}>{prefix}{display}</style>')
            return

        # Wrap the text, first line gets the prefix, continuation lines are indented
        wrapped = textwrap.wrap(display, available)
        if not wrapped:
            return
        tag = f'fg="{color}"'
        if bold:
            tag += ' bold="true"'
        lines.append(f'<style {tag}>{prefix}{wrapped[0]}</style>')
        indent = " " * prefix_len
        for segment in wrapped[1:]:
            lines.append(f'<style {tag}>{indent}{segment}</style>')

    def handle_key(self, event) -> bool:
        """Handle a key event forwarded from the prompt_toolkit application."""
        key_name = self._extract_key_name(event)

        # Editing custom input mode — delegate to specialized handler
        if self._editing_custom_input:
            return self._handle_editing_key(event, key_name)

        # Navigation mode
        if key_name == "up":
            if self.selected_indices[self.current_question_idx] > 0:
                self.selected_indices[self.current_question_idx] -= 1
            return True
        elif key_name == "down":
            opts = self.questions[self.current_question_idx].get("options", [])
            if self.selected_indices[self.current_question_idx] < len(opts) - 1:
                self.selected_indices[self.current_question_idx] += 1
            return True
        elif key_name == "left" and len(self.questions) > 1:
            if self.current_question_idx > 0:
                self.current_question_idx -= 1
            return True
        elif key_name == "right" and len(self.questions) > 1:
            if self.current_question_idx < len(self.questions) - 1:
                self.current_question_idx += 1
            return True
        elif key_name == "enter":
            self._handle_enter()
            return True
        elif key_name == " ":
            self._handle_space()
            return True
        elif key_name == "escape":
            self.cancel()
            return True
        elif key_name in ("backspace", "delete"):
            return True  # no-op in navigation mode

        return False

    # ------------------------------------------------------------------
    # Key extraction helpers (duck-type prompt_toolkit events)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_key_name(event: object) -> Optional[str]:
        """Extract a normalized key name from a prompt_toolkit KeyPressEvent.

        Inspects the last key press in the sequence (``key_sequence[-1]``).
        Prefers ``Keys`` enum ``.name``, falling back to ``.data`` for
        printable characters when no enum name is available.
        """
        try:
            seq = getattr(event, "key_sequence", None)
            if seq:
                press = seq[-1]
                key = getattr(press, "key", None)
                name = getattr(key, "name", None) if key is not None else None
                if not name:
                    name = getattr(press, "data", None)
                return SelectionPanel._normalize_key_name(name) if name else None
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_key_data(event: object) -> Optional[str]:
        """Extract printable character data from a prompt_toolkit KeyPressEvent.

        ``.data`` lives on the ``KeyPress`` object (not on ``.key``).
        """
        try:
            seq = getattr(event, "key_sequence", None)
            if seq:
                press = seq[-1]
                data = getattr(press, "data", None)
                return data if data else None
        except Exception:
            pass
        return None

    @staticmethod
    def _normalize_key_name(name: str) -> str:
        """Normalize key names with platform-dependent variants."""
        name = name.lower()
        if name in ("c-m", "controlm", "\r", "\n"):
            return "enter"
        if name in ("c-h", "controlh"):
            return "backspace"
        if name in ("c-i", "controli", "\t"):
            return "tab"
        if name in ("space",):
            return " "
        if name in ("delete", "c-d"):
            return "delete"
        return name

    # ------------------------------------------------------------------
    # Key-handling helpers
    # ------------------------------------------------------------------

    def _handle_editing_key(self, event, key_name: str) -> bool:
        """Handle keys while editing custom input text."""
        if key_name == "escape":
            self._editing_custom_input = False
            self._custom_input_texts.pop(self.current_question_idx, None)
            return True
        elif key_name == "enter":
            typed = self._custom_input_texts.get(self.current_question_idx, "").strip()
            if not typed:
                return True  # stay in editing mode
            q_idx = self.current_question_idx
            if self._is_multi_select(q_idx):
                self.selections[q_idx] = self._selected_multi_values(q_idx)
            else:
                self.selections[q_idx] = typed
            self._editing_custom_input = False
            self._advance_or_finish()
            return True
        elif key_name == "backspace":
            q_idx = self.current_question_idx
            current = self._custom_input_texts.get(q_idx, "")
            if current:
                self._custom_input_texts[q_idx] = current[:-1]
            return True
        elif key_name == "delete":
            q_idx = self.current_question_idx
            current = self._custom_input_texts.get(q_idx, "")
            if current:
                self._custom_input_texts[q_idx] = current[:-1]
            return True
        elif key_name == " ":
            q_idx = self.current_question_idx
            self._custom_input_texts[q_idx] = (
                self._custom_input_texts.get(q_idx, "") + " "
            )
            return True
        else:
            # Printable character
            data = self._extract_key_data(event)
            if data and len(data) == 1 and ord(data) >= 32:
                q_idx = self.current_question_idx
                current = self._custom_input_texts.get(q_idx, "")
                self._custom_input_texts[q_idx] = current + data
                return True
        return False

    def _handle_enter(self) -> None:
        """Handle Enter in navigation mode: enter edit mode, confirm multi-select,
        or store single-select value and advance."""
        if self._is_custom_input_selected():
            self._custom_input_texts[self.current_question_idx] = ""
            self._editing_custom_input = True
        elif self._is_multi_select():
            q_idx = self.current_question_idx
            selection = self._selected_multi_values(q_idx)
            if not selection:
                return  # nothing selected — ignore Enter
            self.selections[q_idx] = selection
            self._advance_or_finish()
        else:
            options = self.questions[self.current_question_idx].get("options", [])
            if (
                options
                and self.selected_indices[self.current_question_idx] < len(options)
            ):
                self.selections[self.current_question_idx] = options[
                    self.selected_indices[self.current_question_idx]
                ].get("value")
            self._advance_or_finish()

    def _handle_space(self) -> None:
        """Toggle checkbox for multi-select questions."""
        if not self._is_multi_select():
            return
        q_idx = self.current_question_idx
        opt_idx = self.selected_indices[q_idx]
        options = self.questions[q_idx].get("options", [])
        if (
            opt_idx < len(options)
            and options[opt_idx].get("value") != CUSTOM_INPUT_SENTINEL
        ):
            checked = self._checked_indices.get(q_idx, set())
            if opt_idx in checked:
                checked.discard(opt_idx)
            else:
                checked.add(opt_idx)

    def _advance_or_finish(self) -> None:
        """Move to the next question or signal completion via ``finish()``."""
        if self.current_question_idx < len(self.questions) - 1:
            self.current_question_idx += 1
            self._editing_custom_input = False
        else:
            # All questions answered — finish
            if len(self.questions) == 1:
                self.finish(self.selections[0])
            else:
                self.finish(self.selections)

    # ------------------------------------------------------------------
    # Runner (agent-turn context — toolbar-hosted Application)
    # ------------------------------------------------------------------

    def run(self) -> Optional[str | List[str] | int]:
        """Run the toolbar-hosted selection panel and wait for user input.

        Returns:
            Single question: Selected value (str), or None if canceled,
            or 130 if interrupted by pending swarm approval.
            Multi-question: List of selected values, or None/130.
        """
        # Pre-check: if swarm work is already pending, skip the
        # interaction entirely to avoid unnecessary UI startup.
        if self._chat_manager is not None and self._chat_manager.has_pending_swarm_work():
            return 130

        result = run_toolbar_interaction(self, chat_manager=self._chat_manager)

        # run_toolbar_interaction returns None for both user cancel and
        # swarm interrupt (it calls interaction.cancel() on 130).  Use
        # has_pending_swarm_work() to distinguish them.
        #
        # Narrow race: user cancels, then swarm work arrives between
        # cancel() and this check → we return 130 instead of None.  In
        # practice harmless — the caller retries, user sees the swarm
        # prompt and re-cancels if desired.
        if result is None:
            if self._chat_manager is not None and self._chat_manager.has_pending_swarm_work():
                return 130
            return None

        return result


@tool(
    name="select_option",
    description="Ask the user a question with selectable options using arrow keys. A compact toolbar panel shows options navigable with arrow keys. A 'Type your own input...' option is auto-appended for free-form answers. Supports single and multi-question forms (single = array with 1 item). Set 'multi_select': true on a question to allow the user to check multiple options with Space and confirm with Enter.",
    parameters={
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "description": "List of questions (single = array with 1 item, multi = array with multiple items).",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "The question text"},
                        "multi_select": {"type": "boolean", "description": "If true, user can select multiple options using Space. Defaults to false (single-select)."},
                        "options": {
                            "type": "array",
                            "description": "List of options for this question",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "value": {"type": "string", "description": "Value to return if this option is selected"},
                                    "text": {"type": "string", "description": "Display text for the option"},
                                    "description": {"type": "string", "description": "Optional detailed description"}
                                },
                                "required": ["value", "text"]
                            }
                        }
                    },
                    "required": ["question", "options"]
                }
            }
        },
        "required": ["questions"]
    },
    requires_approval=False,
    terminal_policy="yield"
)
def select_option(
    questions: List[Dict[str, Any]],
    context: Dict[str, Any] = None
) -> str:
    """Present a toolbar-hosted selection panel to the user.

    Creates a prompt_toolkit-based selection panel rendered in the bottom
    toolbar where the user can navigate options with arrow keys and select
    by pressing Enter. Pressing Esc cancels.

    Args:
        questions: List of question objects, each containing:
            - question: The question text
            - options: List of option objects with value, text, and optional description
            - multi_select: (optional) If true, user can toggle multiple options with Space
        context: Tool execution context (contains chat_manager)

    Returns:
        str: Formatted tool result with exit_code and selected value(s):
            - "exit_code=0\\n{value}" for single question (1 item in array)
            - "exit_code=0\\n{value1, value2, ...}" for multi-question or multi-select
            - "exit_code=2\\nUser canceled selection" for user cancellation
            - "exit_code=1\\n{error_message}" for validation errors
    """
    try:
        # Validate questions parameter
        if not isinstance(questions, list):
            return "exit_code=1\nQuestions must be a list"

        if not questions:
            return "exit_code=1\nQuestions list cannot be empty"

        # Validate each question
        for q_idx, q in enumerate(questions):
            if not isinstance(q, dict):
                return f"exit_code=1\nQuestion {q_idx + 1} must be an object"

            question_text = q.get("question")
            q_options = q.get("options")

            if not question_text:
                return f"exit_code=1\nQuestion {q_idx + 1} must have a 'question' field"

            if not q_options or not isinstance(q_options, list):
                return f"exit_code=1\nQuestion {q_idx + 1} must have a non-empty 'options' list"

            # Validate each option in the question
            for opt_idx, opt in enumerate(q_options):
                if not isinstance(opt, dict):
                    return f"exit_code=1\nOption {opt_idx + 1} in question {q_idx + 1} must be an object"

                value = opt.get("value")
                text = opt.get("text")

                if not value or not text:
                    return f"exit_code=1\nOption {opt_idx + 1} in question {q_idx + 1} must have 'value' and 'text' fields"

        # Always append custom input option to each question
        for q in questions:
            q["options"] = list(q["options"]) + [CUSTOM_INPUT_OPTION]

        # Extract chat_manager from context for admin interrupt polling
        chat_manager = (context or {}).get("chat_manager")

        # Create and run the selection panel
        panel = SelectionPanel(questions, chat_manager=chat_manager)
        result = panel.run()

        # Handle admin interrupt (pending swarm approval arrived)
        if result == 130:
            return "exit_code=130\nInterrupted by pending swarm approval"

        # Handle user cancellation — exit_code=2 is the universal
        # convention for user-initiated cancellation across all tools.
        if result is None:
            return "exit_code=2\nUser canceled selection"

        console = (context or {}).get("console")

        # Return the selected values (single string for 1 question, comma-separated for multiple)
        if isinstance(result, str):
            if console:
                console.print(f"[dim]Selected: {result}[/dim]", highlight=False)
            return f"exit_code=0\n{result}"
        else:
            # Result is a list (multi-question mode or multi-select)
            formatted = []
            for r in result:
                if isinstance(r, list):
                    # Multi-select question: comma-separated values
                    formatted.append(', '.join(str(v) for v in r))
                else:
                    formatted.append(str(r))
            selected_text = ', '.join(formatted)
            if console:
                console.print(f"[dim]Selected: {selected_text}[/dim]", highlight=False)
            return f"exit_code=0\n{selected_text}"

    except Exception as e:
        return f"exit_code=1\nError displaying selection panel: {str(e)}"
