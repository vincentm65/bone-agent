"""Interactive selection tool for presenting multiple-choice questions to the user."""

from threading import Timer
from typing import Optional, List, Dict, Any, Union

from prompt_toolkit import HTML
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout, HSplit, Window
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.layout.controls import FormattedTextControl

from ui.prompt_utils import TOOLBAR_STYLE

from .helpers.base import tool

# Sentinel value used to detect when user selects the custom input option
CUSTOM_INPUT_SENTINEL = "__custom_input__"
CUSTOM_INPUT_OPTION = {
    "value": CUSTOM_INPUT_SENTINEL,
    "text": "Type your own input...",
    "description": "Provide your own answer instead of selecting from the list"
}


class SelectionPanel:
    """Inline selection panel with arrow key navigation and inline custom input."""

    # Cursor indicator
    _CURSOR = "> "

    def __init__(self, questions: List[Dict[str, Any]]):
        """Initialize the selection panel.

        Args:
            questions: List of question dicts with 'question', 'options' (each with 'value', 'text', optional 'description')
        """
        self.questions = questions
        self._showing_summary = False

        # Initialize for multi-question mode (handles both single and multiple questions)
        self.current_question_idx = 0
        self.selections = [None] * len(questions)
        # Initialize selected_index for each question
        self.selected_indices = [0] * len(questions)

        # Inline custom input editing state
        self._editing_custom_input = False
        self._custom_input_texts: Dict[int, str] = {}  # question_idx -> typed text

    def _is_custom_input_selected(self) -> bool:
        """Check if the custom input option is currently selected."""
        q_idx = self.current_question_idx
        options = self.questions[q_idx].get("options", [])
        opt_idx = self.selected_indices[q_idx]
        if opt_idx < len(options):
            return options[opt_idx].get("value") == CUSTOM_INPUT_SENTINEL
        return False

    def _wrap_description(self, text: str, indent: str, width: int = None) -> List[str]:
        """Wrap description text preserving indent on continuation lines.

        Args:
            text: The description text to wrap.
            indent: The indentation string (e.g. '   ').
            width: Maximum line width. Defaults to current terminal width.

        Returns:
            List of lines, first without extra indent, continuations with indent.
        """
        import os
        if width is None:
            width = os.get_terminal_size().columns
        available = width - len(indent)
        if available <= 0:
            return [text]
        words = text.split()
        lines = []
        current = ""
        for word in words:
            if not current:
                current = word
            elif len(current) + 1 + len(word) <= available:
                current += " " + word
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    def _get_display_text(self) -> HTML:
        """Get the formatted text to display.

        Returns:
            HTML formatted text with current selection state
        """
        lines = []

        # Single question mode (1 item in array)
        if len(self.questions) == 1:
            # Check if showing summary
            if self._showing_summary:
                lines.append("<b>Selection Summary</b>")
                lines.append("")

                question = self.questions[0].get("question", "")
                selected_value = self.selections[0] if self.selections else None
                options = self.questions[0].get("options", [])

                # Find the option text for the selected value
                selected_opt = next((opt for opt in options if opt.get("value") == selected_value), None)
                selected_text = selected_opt.get("text", selected_value) if selected_opt else selected_value

                lines.append(f"<b>Question:</b> {question}")
                lines.append(f'<style fg="gray">  Selected: {str(selected_text)}</style>')
                lines.append("")
            else:
                question = self.questions[0]
                question_text = question.get("question", "")
                options = question.get("options", [])

                # Show single question
                lines.append(f"<b>{question_text}</b>")
                lines.append("")

                # Render options
                for o_idx, opt in enumerate(options):
                    text = opt.get("text", "")
                    description = opt.get("description", "")

                    if o_idx == self.selected_indices[0]:
                        # Selected option - show cursor and highlight
                        if opt.get("value") == CUSTOM_INPUT_SENTINEL and self._editing_custom_input:
                            # Editing mode: show text field with user input
                            typed = self._custom_input_texts.get(0, "")
                            lines.append(f'<style fg="white" bold="true">{self._CURSOR}> {typed}</style>')
                            lines.append(f'<style fg="gray">   Type your answer, Enter to confirm, Esc to go back</style>')
                        else:
                            # Navigation mode
                            if opt.get("value") == CUSTOM_INPUT_SENTINEL:
                                typed = self._custom_input_texts.get(0, "")
                                display = typed if typed else text
                            else:
                                display = text
                            lines.append(f'<style fg="white" bold="true">{self._CURSOR}{display}</style>')
                            if description:
                                for i, wl in enumerate(self._wrap_description(description, "   ")):
                                    lines.append(f'<style fg="white">   {wl}</style>')
                    else:
                        # Unselected option - dark grey
                        if opt.get("value") == CUSTOM_INPUT_SENTINEL:
                            typed = self._custom_input_texts.get(0, "")
                            display = typed if typed else text
                        else:
                            display = text
                        lines.append(f'<style fg="gray">  {display}</style>')
                        if description:
                            for i, wl in enumerate(self._wrap_description(description, "   ")):
                                lines.append(f'<style fg="gray">   {wl}</style>')

                # Add help text
                lines.append("")
                if self._editing_custom_input:
                    lines.append('<style fg="gray">Type your answer. Enter to confirm, Esc to go back</style>')
                else:
                    lines.append('<style fg="gray">Use ↑↓ to navigate, Enter to confirm, Esc to cancel</style>')
        # Multi-question mode (multiple items in array)
        else:
            # Check if showing summary
            if self._showing_summary:
                lines.append("<b>Selections Summary</b>")
                lines.append("")

                for q_idx, q in enumerate(self.questions):
                    question = q.get("question", "")
                    selected_value = self.selections[q_idx] if q_idx < len(self.selections) else None
                    options = q.get("options", [])

                    # Find the option text for the selected value
                    selected_opt = next((opt for opt in options if opt.get("value") == selected_value), None)
                    selected_text = selected_opt.get("text", selected_value) if selected_opt else selected_value

                    lines.append(f"<b>Question {q_idx + 1}:</b> {question}")
                    lines.append(f'<style fg="gray">  Selected: {str(selected_text)}</style>')
                    lines.append("")
            else:
                question = self.questions[self.current_question_idx]
                question_text = question.get("question", "")
                options = question.get("options", [])
                q_num = self.current_question_idx + 1
                q_total = len(self.questions)

                # Show only current question
                lines.append(f"<b>Question {q_num}/{q_total}: {question_text}</b>")
                lines.append("")

                # Render options for current question only
                for o_idx, opt in enumerate(options):
                    text = opt.get("text", "")
                    description = opt.get("description", "")

                    if o_idx == self.selected_indices[self.current_question_idx]:
                        # Selected option - show cursor and highlight
                        if opt.get("value") == CUSTOM_INPUT_SENTINEL and self._editing_custom_input:
                            # Editing mode: show text field with user input
                            typed = self._custom_input_texts.get(self.current_question_idx, "")
                            lines.append(f'<style fg="white" bold="true">{self._CURSOR}> {typed}</style>')
                            lines.append(f'<style fg="gray">   Type your answer, Enter to confirm, Esc to go back</style>')
                        else:
                            # Navigation mode
                            if opt.get("value") == CUSTOM_INPUT_SENTINEL:
                                typed = self._custom_input_texts.get(self.current_question_idx, "")
                                display = typed if typed else text
                            else:
                                display = text
                            lines.append(f'<style fg="white" bold="true">{self._CURSOR}{display}</style>')
                            if description:
                                for i, wl in enumerate(self._wrap_description(description, "   ")):
                                    lines.append(f'<style fg="white">   {wl}</style>')
                    else:
                        # Unselected option - dark grey
                        if opt.get("value") == CUSTOM_INPUT_SENTINEL:
                            typed = self._custom_input_texts.get(self.current_question_idx, "")
                            display = typed if typed else text
                        else:
                            display = text
                        lines.append(f'<style fg="gray">  {display}</style>')
                        if description:
                            for i, wl in enumerate(self._wrap_description(description, "   ")):
                                lines.append(f'<style fg="gray">   {wl}</style>')

                # Add help text
                lines.append("")
                if self._editing_custom_input:
                    lines.append('<style fg="gray">Type your answer. Enter to confirm, Esc to go back</style>')
                else:
                    lines.append('<style fg="gray">Use ↑↓ to navigate options, ←→ for questions, Enter to confirm, Esc to cancel</style>')

        return HTML("\n".join(lines))

    def _advance_question(self, event) -> None:
        """Advance to next question or finish.

        Args:
            event: PromptToolkit event object
        """
        if len(self.questions) == 1:
            # Single question - show summary then auto-exit
            self._showing_summary = True
            event.app.invalidate()
            Timer(1.0, lambda: event.app.exit(result=self.selections[0])).start()
        else:
            # Multi-question - advance or finish
            if self.current_question_idx < len(self.questions) - 1:
                self.current_question_idx += 1
                self._editing_custom_input = False
                event.app.invalidate()
            else:
                self._showing_summary = True
                event.app.invalidate()
                Timer(1.0, lambda: event.app.exit(result=self.selections)).start()

    def run(self) -> Optional[Union[str, List[str]]]:
        """Display the selection panel and wait for user input.

        Returns:
            Single question mode: Selected value (str), or None if canceled
            Multi-question mode: List of selected values (List[str]), or None if canceled
        """
        # Create key bindings for navigation
        bindings = KeyBindings()

        @bindings.add(Keys.Up)
        def move_up(event):
            """Move selection up."""
            if self._showing_summary or self._editing_custom_input:
                return
            if self.selected_indices[self.current_question_idx] > 0:
                self.selected_indices[self.current_question_idx] -= 1
            event.app.invalidate()

        @bindings.add(Keys.Down)
        def move_down(event):
            """Move selection down."""
            if self._showing_summary or self._editing_custom_input:
                return
            current_options = self.questions[self.current_question_idx].get("options", [])
            if self.selected_indices[self.current_question_idx] < len(current_options) - 1:
                self.selected_indices[self.current_question_idx] += 1
            event.app.invalidate()

        @bindings.add(Keys.Left)
        def prev_question(event):
            """Go to previous question (multi-question mode only)."""
            if self._showing_summary or self._editing_custom_input:
                return
            if len(self.questions) > 1 and self.current_question_idx > 0:
                self.current_question_idx -= 1
                event.app.invalidate()

        @bindings.add(Keys.Right)
        def next_question(event):
            """Go to next question (multi-question mode only)."""
            if self._showing_summary or self._editing_custom_input:
                return
            if len(self.questions) > 1 and self.current_question_idx < len(self.questions) - 1:
                self.current_question_idx += 1
                event.app.invalidate()

        @bindings.add(Keys.Enter)
        def select(event):
            """Confirm selection or toggle custom input editing."""
            if self._showing_summary:
                return

            if self._editing_custom_input:
                # Confirm custom input text
                typed = self._custom_input_texts.get(self.current_question_idx, "").strip()
                if not typed:
                    # Empty input - go back to editing, don't advance
                    return
                self.selections[self.current_question_idx] = typed
                self._editing_custom_input = False
                self._advance_question(event)
            else:
                # Check if custom input option is selected
                if self._is_custom_input_selected():
                    # Enter edit mode
                    self._editing_custom_input = True
                    event.app.cursor_position = (0, 0)  # Reset cursor
                    event.app.invalidate()
                else:
                    # Regular option - store and advance
                    current_options = self.questions[self.current_question_idx].get("options", [])
                    if current_options and self.selected_indices[self.current_question_idx] < len(current_options):
                        self.selections[self.current_question_idx] = current_options[self.selected_indices[self.current_question_idx]].get("value")
                    self._advance_question(event)

        @bindings.add(Keys.Escape)
        def cancel(event):
            """Cancel editing or cancel selection."""
            if self._editing_custom_input:
                # Exit editing mode, return to navigation
                self._editing_custom_input = False
                event.app.invalidate()
            else:
                # Cancel entire selection
                event.app.exit(result=None)

        # Printable character input for custom input editing
        @bindings.add(Keys.Any)
        def handle_input(event):
            """Handle printable character input when editing custom input."""
            if not self._editing_custom_input or self._showing_summary:
                return

            data = event.data
            # Filter to printable characters (no control chars)
            if len(data) == 1 and ord(data) >= 32:
                q_idx = self.current_question_idx
                current = self._custom_input_texts.get(q_idx, "")
                self._custom_input_texts[q_idx] = current + data
                event.app.invalidate()

        @bindings.add(Keys.Backspace)
        def handle_backspace(event):
            """Handle backspace when editing custom input."""
            if not self._editing_custom_input or self._showing_summary:
                return
            q_idx = self.current_question_idx
            current = self._custom_input_texts.get(q_idx, "")
            if current:
                self._custom_input_texts[q_idx] = current[:-1]
                event.app.invalidate()

        @bindings.add(Keys.Delete)
        def handle_delete(event):
            """Handle delete when editing custom input."""
            if not self._editing_custom_input or self._showing_summary:
                return
            # Delete at cursor position - for simplicity, same as backspace
            # since we don't track cursor position within the text
            q_idx = self.current_question_idx
            current = self._custom_input_texts.get(q_idx, "")
            if current:
                self._custom_input_texts[q_idx] = current[:-1]
                event.app.invalidate()

        # Create the content control
        def get_content():
            return self._get_display_text()

        content_control = FormattedTextControl(get_content)

        # Create layout with the content
        root_container = HSplit([
            Window(content=content_control, height=D(min=1), width=D(min=1), wrap_lines=True),
        ])

        layout = Layout(root_container)

        # Create and run the application
        application = Application(
            layout=layout,
            key_bindings=bindings,
            full_screen=False,
            mouse_support=False,
            cursor=None,
            style=TOOLBAR_STYLE,
        )

        result = application.run()

        return result


@tool(
    name="select_option",
    description="Ask the user a question with selectable options using arrow keys. Displays an inline panel where the user navigates with arrow keys and presses Enter to select. A 'Type your own input...' option is automatically appended as the last option, allowing the user to provide a free-form answer. Set 'allow_custom_input' to false on a question to disable this. Useful for clarifying requirements, making decisions, or getting user preferences. Supports both single question and multi-question forms (single question = array with 1 item).",
    parameters={
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "description": "List of questions. Single question mode: array with 1 item. Multi-question mode: array with multiple items.",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "The question text"},
                        "allow_custom_input": {"type": "boolean", "description": "If true (default), appends a 'Type your own input...' option as the last option, letting the user provide a free-form answer. Set to false to disable."},
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
    allowed_modes=["edit", "plan"],
    requires_approval=False
)
def select_option(
    questions: List[Dict[str, Any]],
    context: Dict[str, Any] = None
) -> str:
    """Present an inline selection panel to the user.

    Creates a prompt_toolkit-based selection panel where the user can navigate
    options with arrow keys and select by pressing Enter. Pressing Esc cancels.

    Args:
        questions: List of question objects, each containing:
            - question: The question text
            - options: List of option objects with value, text, and optional description
        context: Tool execution context (contains chat_manager)

    Returns:
        str: Formatted tool result with exit_code and selected value(s):
            - "exit_code=0\\n{value}" for single question (1 item in array)
            - "exit_code=0\\n{value1, value2, ...}" for multi-question (comma-separated list)
            - "exit_code=1\\n{error_message}" for user cancellation or validation errors
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

        # Append custom input option to each question that allows it
        for q in questions:
            if q.get("allow_custom_input", True):
                q["options"] = list(q["options"]) + [CUSTOM_INPUT_OPTION]

        # Create and run the selection panel
        panel = SelectionPanel(questions)
        result = panel.run()

        # Handle user cancellation
        if result is None:
            return "exit_code=1\nUser canceled selection"

        # Return the selected values (single string for 1 question, comma-separated for multiple)
        if isinstance(result, str):
            return f"exit_code=0\n{result}"
        else:
            return f"exit_code=0\n{', '.join(str(r) for r in result)}"

    except Exception as e:
        return f"exit_code=1\nError displaying selection panel: {str(e)}"
