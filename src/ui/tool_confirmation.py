"""Interactive tool confirmation panel with arrow key navigation."""

import asyncio
from threading import Timer
from typing import Optional, Tuple
from prompt_toolkit import HTML
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl




class ToolConfirmationPanel:
    """Interactive panel for tool execution confirmation with arrow key navigation."""

    # Public constants
    SUMMARY_DISPLAY_DELAY = 0.5  # Seconds to show summary before auto-exit
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

    def __init__(self, tool_command: str, reason: Optional[str] = None, is_edit_tool: bool = False, cycle_approve_mode=None):
        """Initialize the tool confirmation panel.

        Args:
            tool_command: Command/tool being executed
            reason: Optional reason/details about the tool execution
            is_edit_tool: Whether this is an edit tool (shows extra toggle option)
            cycle_approve_mode: Optional callback to cycle approve_mode
        """
        self.tool_command = tool_command
        self.reason = reason
        self.is_edit_tool = is_edit_tool
        self.cycle_approve_mode = cycle_approve_mode
        self.selected_index = 0
        self._showing_summary = False
        self._selected_value = None
        # Use appropriate options based on tool type
        self._options = self.EDIT_OPTIONS if is_edit_tool else self.STANDARD_OPTIONS

    def _get_display_text(self) -> HTML:
        """Get the formatted text to display.

        Returns:
            HTML formatted text with current selection state
        """
        lines = []

        # Check if showing summary
        if self._showing_summary:
            # Find the option text for the selected value
            selected_opt = next((opt for opt in self._options if opt.get("value") == self._selected_value), None)
            selected_text = selected_opt.get("text", self._selected_value) if selected_opt else self._selected_value

            lines.append("<b>Selection Summary</b>")
            lines.append("")
            lines.append(f"<b>Tool:</b> {self.tool_command}")
            lines.append(f'<style fg="gray">  Selected: {str(selected_text)}</style>')
            lines.append("")
        else:
            # Tool information
            lines.append(f"<b>Tool:</b> {self.tool_command}")
            if self.reason:
                lines.append(f"<b>Reason:</b> {self.reason}")
            lines.append("")

            # Render options
            for idx, opt in enumerate(self._options):
                text = opt.get("text", "")

                if idx == self.selected_index:
                    # Selected option - show cursor and highlight in bold white
                    lines.append(f'<style fg="white" bold="true">{self.CURSOR}{text}</style>')
                else:
                    # Unselected option - dark grey
                    lines.append(f'<style fg="gray">  {text}</style>')

            return HTML("\n".join(lines))

    def _exit_with_summary(self, event, result: str) -> None:
        """Show summary screen and exit application after delay.

        Args:
            event: PromptToolkit event object
            result: Result value to return when application exits
        """
        if self._showing_summary:
            return
        self._showing_summary = True
        self._selected_value = result
        event.app.invalidate()
        Timer(self.SUMMARY_DISPLAY_DELAY, lambda: event.app.exit(result=result)).start()

    def _create_key_bindings(self) -> KeyBindings:
        """Create key bindings for navigation and selection.

        Returns:
            KeyBindings object with Up/Down/Enter/Esc handlers
        """
        bindings = KeyBindings()

        @bindings.add(Keys.Up)
        def move_up(event):
            """Move selection up."""
            if self.selected_index > 0:
                self.selected_index -= 1
            event.app.invalidate()

        @bindings.add(Keys.Down)
        def move_down(event):
            """Move selection down."""
            if self.selected_index < len(self._options) - 1:
                self.selected_index += 1
            event.app.invalidate()

        @bindings.add(Keys.Enter)
        def select(event):
            """Confirm selection."""
            selected_value = self._options[self.selected_index].get("value")
            self._selected_value = selected_value

            # Handle accept_all_edits option
            if selected_value == "accept_all_edits":
                # Call the callback to cycle approve_mode
                if self.cycle_approve_mode:
                    self.cycle_approve_mode()
                # Return "accept" so the current edit proceeds
                self._exit_with_summary(event, "accept")
            else:
                # Show summary then auto-exit
                self._exit_with_summary(event, selected_value)

        @bindings.add(Keys.Escape)
        def cancel(event):
            """Cancel selection."""
            self._exit_with_summary(event, "cancel")

        return bindings

    def _create_layout(self) -> Layout:
        """Create the panel layout.

        Returns:
            Layout object configured for the confirmation panel
        """
        def get_content():
            return self._get_display_text()

        content_control = FormattedTextControl(get_content)

        root_container = HSplit([
            Window(content=content_control, height=None),
        ])

        return Layout(root_container)

    def _handle_result(self, result: str) -> Tuple[str, Optional[str]]:
        """Handle the result from the confirmation panel.

        Args:
            result: The action selected by the user

        Returns:
            Tuple of (action, guidance_text):
                - action: "accept", "advise", "accept_all_edits", or "cancel"
                - guidance_text: User's advice if action is "advise", None otherwise
        """
        # Handle advise input separately
        if result == "advise":
            from prompt_toolkit import PromptSession
            from prompt_toolkit.formatted_text import HTML

            guidance_session = PromptSession()
            guidance = guidance_session.prompt(HTML("<b>Enter your advice: </b>")).strip()
            if not guidance:
                # Empty advise treated as cancel
                return ("cancel", None)
            return ("advise", guidance)

        return (result, None)

    def run(self) -> Tuple[str, Optional[str]]:
        """Display the confirmation panel and wait for user input.

        Returns:
            Tuple of (action, guidance_text):
                - action: "accept", "advise", "accept_all_edits", or "cancel"
                - guidance_text: User's advice if action is "advise", None otherwise
        """
        # Create and run the application
        bindings = self._create_key_bindings()
        layout = self._create_layout()

        application = Application(
            layout=layout,
            key_bindings=bindings,
            full_screen=False,
            mouse_support=False,
        )

        # Use run_async with asyncio to properly await coroutines
        result = asyncio.run(application.run_async())
        return self._handle_result(result)
