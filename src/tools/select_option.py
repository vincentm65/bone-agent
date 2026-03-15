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


class SelectionPanel:
    """Inline selection panel with arrow key navigation."""

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
                        # Selected option - show cursor and highlight in bold white
                        lines.append(f'<style fg="white" bold="true">{self._CURSOR}{text}</style>')
                        if description:
                            lines.append(f'<style fg="white">   {description}</style>')
                    else:
                        # Unselected option - dark grey
                        lines.append(f'<style fg="gray">  {text}</style>')
                        if description:
                            lines.append(f'<style fg="gray">   {description}</style>')

                # Add help text
                lines.append("")
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
                        # Selected option - show cursor and highlight in bold white
                        lines.append(f'<style fg="white" bold="true">{self._CURSOR}{text}</style>')
                        if description:
                            lines.append(f'<style fg="white">   {description}</style>')
                    else:
                        # Unselected option - dark grey
                        lines.append(f'<style fg="gray">  {text}</style>')
                        if description:
                            lines.append(f'<style fg="gray">   {description}</style>')

                # Add help text
                lines.append("")
                lines.append('<style fg="gray">Use ↑↓ to navigate options, ←→ for questions, Enter to confirm, Esc to cancel</style>')

        return HTML("\n".join(lines))



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
            if self._showing_summary:
                return  # Disable navigation when showing summary
            # Multi-question mode - move within current question (works for single question too)
            if self.selected_indices[self.current_question_idx] > 0:
                self.selected_indices[self.current_question_idx] -= 1
            event.app.invalidate()

        @bindings.add(Keys.Down)
        def move_down(event):
            """Move selection down."""
            if self._showing_summary:
                return  # Disable navigation when showing summary
            # Multi-question mode - move within current question (works for single question too)
            current_options = self.questions[self.current_question_idx].get("options", [])
            if self.selected_indices[self.current_question_idx] < len(current_options) - 1:
                self.selected_indices[self.current_question_idx] += 1
            event.app.invalidate()

        @bindings.add(Keys.Left)
        def prev_question(event):
            """Go to previous question (multi-question mode only)."""
            if self._showing_summary:
                return  # Disable navigation when showing summary
            if self.questions is not None and len(self.questions) > 1:
                if self.current_question_idx > 0:
                    self.current_question_idx -= 1
                event.app.invalidate()

        @bindings.add(Keys.Right)
        def next_question(event):
            """Go to next question (multi-question mode only)."""
            if self._showing_summary:
                return  # Disable navigation when showing summary
            if self.questions is not None and len(self.questions) > 1:
                if self.current_question_idx < len(self.questions) - 1:
                    self.current_question_idx += 1
                event.app.invalidate()

        @bindings.add(Keys.Enter)
        def select(event):
            """Confirm selection or move to next question."""
            # Multi-question mode - sequential
            # Store current selection
            current_options = self.questions[self.current_question_idx].get("options", [])
            if current_options and self.selected_indices[self.current_question_idx] < len(current_options):
                self.selections[self.current_question_idx] = current_options[self.selected_indices[self.current_question_idx]].get("value")

            # Single question mode (1 item in array)
            if len(self.questions) == 1:
                # All questions answered - show summary then auto-exit
                self._showing_summary = True
                event.app.invalidate()
                # Auto-exit after 1 second
                Timer(1.0, lambda: event.app.exit(result=self.selections[0])).start()
            # Multi-question mode (multiple items in array)
            else:
                # Move to next question or show summary if done
                if self.current_question_idx < len(self.questions) - 1:
                    # More questions - move to next
                    self.current_question_idx += 1
                    event.app.invalidate()
                else:
                    # All questions answered - show summary then auto-exit
                    self._showing_summary = True
                    event.app.invalidate()
                    # Auto-exit after 1 second
                    Timer(1.0, lambda: event.app.exit(result=self.selections)).start()

        @bindings.add(Keys.Escape)
        def cancel(event):
            """Cancel selection."""
            self._user_response = None
            event.app.exit(result=None)

        # Create the content control
        def get_content():
            return self._get_display_text()

        content_control = FormattedTextControl(get_content)

        # Create layout with the content
        root_container = HSplit([
            Window(content=content_control, height=None),
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
    description="Ask the user a question with selectable options using arrow keys. Displays an inline panel where the user navigates with arrow keys and presses Enter to select. Useful for clarifying requirements, making decisions, or getting user preferences. Supports both single question and multi-question forms (single question = array with 1 item).",
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
    allowed_modes=["edit", "plan", "learn"],
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
