"""Interactive selection tool for presenting multiple-choice questions to the user."""

from typing import Optional, List, Dict

from prompt_toolkit import HTML
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl

from .helpers.base import tool


class SelectionPanel:
    """Inline selection panel with arrow key navigation."""

    # Cursor indicator
    _CURSOR = "> "

    def __init__(self, question: str, options: List[Dict[str, str]], title: str, console=None, questions=None):
        """Initialize the selection panel.

        Args:
            question: The question to ask the user (for single question mode)
            options: List of option dicts with 'value', 'text', and optional 'description' (for single question mode)
            title: Title for the panel
            console: Rich console for display (optional)
            questions: List of question dicts for multi-question mode (overrides question/options)
        """
        self.console = console
        self.questions = questions
        self.title = title
        self._user_response = None
        self._line_count = 0
        self._showing_summary = False

        # Single question mode
        if questions is None:
            self.question = question
            self.options = options
            self.selected_index = 0
            self.current_question_idx = 0
            self.selections = None
        # Multi-question mode
        else:
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

        # Single question mode
        if self.questions is None:
            lines.append(f"<b>{self.question}</b>")
            lines.append("")

            # Render each option
            for idx, opt in enumerate(self.options):
                text = opt.get("text", "")
                description = opt.get("description", "")

                if idx == self.selected_index:
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

        # Multi-question mode - sequential (one question at a time)
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
                    lines.append(f'<style fg="gray">  Selected: {selected_text}</style>')
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

        # Store line count for clearing later
        self._line_count = len(lines)

        return HTML("\n".join(lines))



    def run(self) -> Optional[str]:
        """Display the selection panel and wait for user input.

        Returns:
            Selected value, or None if canceled
        """
        # Create key bindings for navigation
        bindings = KeyBindings()

        @bindings.add(Keys.Up)
        def move_up(event):
            """Move selection up."""
            if self.questions is None:
                # Single question mode
                if self.selected_index > 0:
                    self.selected_index -= 1
            else:
                # Multi-question mode - move within current question
                if self.selected_indices[self.current_question_idx] > 0:
                    self.selected_indices[self.current_question_idx] -= 1
            event.app.invalidate()

        @bindings.add(Keys.Down)
        def move_down(event):
            """Move selection down."""
            if self.questions is None:
                # Single question mode
                if self.selected_index < len(self.options) - 1:
                    self.selected_index += 1
            else:
                # Multi-question mode - move within current question
                current_options = self.questions[self.current_question_idx].get("options", [])
                if self.selected_indices[self.current_question_idx] < len(current_options) - 1:
                    self.selected_indices[self.current_question_idx] += 1
            event.app.invalidate()

        @bindings.add(Keys.Left)
        def prev_question(event):
            """Go to previous question (multi-question mode)."""
            if self.questions is not None and not self._showing_summary:
                if self.current_question_idx > 0:
                    self.current_question_idx -= 1
                event.app.invalidate()

        @bindings.add(Keys.Right)
        def next_question(event):
            """Go to next question (multi-question mode)."""
            if self.questions is not None and not self._showing_summary:
                if self.current_question_idx < len(self.questions) - 1:
                    self.current_question_idx += 1
                event.app.invalidate()

        @bindings.add(Keys.Enter)
        def select(event):
            """Confirm selection or move to next question."""
            if self.questions is None:
                # Single question mode
                self._user_response = self.options[self.selected_index].get("value")
                event.app.exit(result=self._user_response)
            else:
                # Multi-question mode - sequential
                # Store current selection
                current_options = self.questions[self.current_question_idx].get("options", [])
                if current_options and self.selected_indices[self.current_question_idx] < len(current_options):
                    self.selections[self.current_question_idx] = current_options[self.selected_indices[self.current_question_idx]].get("value")

                # Move to next question or auto-submit if done
                if self.current_question_idx < len(self.questions) - 1:
                    # More questions - move to next
                    self.current_question_idx += 1
                    event.app.invalidate()
                else:
                    # All questions answered - auto-submit
                    self._showing_summary = True
                    self._user_response = self.selections
                    event.app.exit(result=self.selections)

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
        )

        result = application.run()

        # Print summary for single question mode (multi-question shows inline)
        if result:
            # Use provided console or create a new one
            if self.console:
                console = self.console
            else:
                from rich.console import Console
                console = Console()

            if self.questions is None:
                # Single question mode - print to console
                selected_opt = next((opt for opt in self.options if opt.get("value") == result), None)
                selected_text = selected_opt.get("text", result) if selected_opt else result

                console.print(f"[cyan]Selection:[/cyan] {self.question}")
                console.print(f"[cyan]Selected:[/cyan] [bold cyan]{selected_text}[/bold cyan]")
                console.print()
            # Multi-question mode - summary shown inline in UI, no console output needed

        return result


@tool(
    name="select_option",
    description="Ask the user a question with selectable options using arrow keys. Displays an inline panel where the user navigates with arrow keys and presses Enter to select. Useful for clarifying requirements, making decisions, or getting user preferences. Supports both single question and multi-question forms.",
    parameters={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to ask the user (for single question mode)"
            },
            "options": {
                "type": "array",
                "description": "List of selectable options (for single question mode)",
                "items": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string", "description": "Value to return if this option is selected"},
                        "text": {"type": "string", "description": "Display text shown to the user for this option"},
                        "description": {"type": "string", "description": "Optional detailed description shown below the option text"}
                    },
                    "required": ["value", "text"]
                }
            },
            "questions": {
                "type": "array",
                "description": "List of questions for multi-question mode (overrides question/options)",
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
            },
            "title": {
                "type": "string",
                "description": "Title for the selection dialog (optional, defaults to 'Select an Option')"
            }
        },
        "required": []
    },
    allowed_modes=["edit", "plan", "learn"],
    requires_approval=False
)
def select_option(
    question: Optional[str] = None,
    options: Optional[List[Dict[str, str]]] = None,
    questions: Optional[List[Dict[str, List[Dict[str, str]]]]] = None,
    title: Optional[str] = None,
    console = None
) -> str:
    """Present an inline selection panel to the user.

    Creates a prompt_toolkit-based selection panel where the user can navigate
    options with arrow keys and select by pressing Enter. Pressing Esc cancels.

    Args:
        question: The question to ask the user (for single question mode)
        options: List of option objects, each containing:
            - value: The value to return if selected
            - text: Display text for the option
            - description: Optional detailed description
        questions: List of question objects for multi-question mode, each containing:
            - question: The question text
            - options: List of option objects
        title: Optional title for the panel (defaults to "Select an Option")
        console: Rich console for display (optional)

    Returns:
        str: Tool result with exit_code and selected value(s)
            - exit_code=0: User selected option(s), value(s) returned
            - exit_code=1: User canceled (pressed Esc) or invalid input
    """
    try:
        # Validate that either single or multi-question mode is provided
        if questions is None:
            # Single question mode - validate question and options
            if not question:
                return "exit_code=1\n'question' is required for single question mode"

            if not options or not isinstance(options, list):
                return "exit_code=1\nOptions must be a non-empty list"

            # Validate each option
            for opt in options:
                if not isinstance(opt, dict):
                    return "exit_code=1\nEach option must be an object"

                value = opt.get("value")
                text = opt.get("text")

                if not value or not text:
                    return "exit_code=1\nEach option must have 'value' and 'text' fields"

            # Set default title
            dialog_title = title or "Select an Option"

            # Create and run the selection panel
            panel = SelectionPanel(question, options, dialog_title, console)
            result = panel.run()

            # Handle user cancellation
            if result is None:
                return "exit_code=1\nUser canceled selection"

            # Return the selected value
            return f"exit_code=0\n{result}"

        else:
            # Multi-question mode
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

            # Set default title
            dialog_title = title or "Select Options"

            # Create and run the selection panel
            panel = SelectionPanel(None, None, dialog_title, console, questions=questions)
            result = panel.run()

            # Handle user cancellation
            if result is None:
                return "exit_code=1\nUser canceled selection"

            # Return the selected values (list)
            return f"exit_code=0\n{', '.join(str(r) for r in result)}"

    except Exception as e:
        return f"exit_code=1\nError displaying selection panel: {str(e)}"
