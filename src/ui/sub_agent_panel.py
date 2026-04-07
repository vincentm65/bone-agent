"""Live panel for streaming sub-agent tool output."""

import logging
import threading
import time

from rich.panel import Panel
from rich.text import Text
from rich.live import Live

from core.tool_feedback import build_panel_tool_message

logger = logging.getLogger(__name__)


class SubAgentPanel:
    """Live panel for streaming sub-agent tool output.

    Displays a Rich panel with animated spinner, tool call log, and
    completion/error status for sub-agent invocations.
    """

    _SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, query, console):
        """Initialize the sub-agent panel.

        Args:
            query: The task query for the sub-agent
            console: Rich console for display
        """
        self.console = console
        self.query = query
        self.tool_calls = []  # List of formatted Rich markup strings
        self.total_tool_calls = 0
        self._live = None
        self._spinner_index = 0
        self._show_spinner = True
        self._spinner_thread = None
        self._stop_spinner = threading.Event()
        self._saved_termios = None

    # ------------------------------------------------------------------
    # Panel rendering
    # ------------------------------------------------------------------

    def _get_title(self):
        """Get panel title with optional spinner and tool call counter."""
        if self._show_spinner:
            spinner = self._SPINNER_FRAMES[self._spinner_index % len(self._SPINNER_FRAMES)]
            return f"[cyan]{spinner} Sub-Agent ({self.total_tool_calls})[/cyan]"
        return f"[cyan]Sub-Agent ({self.total_tool_calls})[/cyan]"

    def _render_panel(self, title=None, border_style="cyan"):
        """Render the current panel state.

        Args:
            title: Optional title override. If None, uses _get_title().
            border_style: Border style (default: "cyan")

        Returns:
            Rich Panel object with current content and title
        """
        lines = [f"[bold cyan]Query:[/bold cyan] {self.query}", ""]

        if self.tool_calls:
            content = "\n".join(self.tool_calls)
            lines.append(content)
        else:
            lines.append("[dim]No tools called yet[/dim]")

        content = "\n".join(lines)
        return Panel(
            Text.from_markup(content, justify="left"),
            title=title if title is not None else self._get_title(),
            title_align="left",
            border_style=border_style,
            padding=(0, 1),
        )

    # ------------------------------------------------------------------
    # Spinner animation
    # ------------------------------------------------------------------

    def _spin(self):
        """Background thread: continuously increment spinner and update display."""
        while not self._stop_spinner.is_set():
            self._spinner_index += 1
            if self._live:
                self._live.update(self._render_panel())
            time.sleep(0.1)  # 10 updates per second = smooth animation

    # ------------------------------------------------------------------
    # Terminal raw mode (suppress keystroke echoes during spinner)
    # ------------------------------------------------------------------

    @staticmethod
    def _set_raw_mode():
        """Switch stdin to raw mode to prevent keystroke echoes during spinner."""
        import os
        import sys
        if os.name == 'nt':
            return
        try:
            import termios
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            new = old.copy()
            new[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN)
            new[0] &= ~(termios.ICRNL)
            termios.tcsetattr(fd, termios.TCSANOW, new)
            return old
        except Exception:
            return None

    @staticmethod
    def _restore_terminal_mode(saved):
        """Restore terminal mode from saved termios attributes."""
        import os
        import sys
        if saved is None:
            return
        try:
            import os as _os
            if _os.name == 'nt':
                return
        except Exception:
            pass
        try:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, saved)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Context manager (Live display lifecycle)
    # ------------------------------------------------------------------

    def __enter__(self):
        """Start Live display context.

        Returns:
            self for use in with statement
        """
        self._saved_termios = self._set_raw_mode()
        panel = self._render_panel()
        self._live = Live(panel, console=self.console, refresh_per_second=10)
        self._live.__enter__()

        # Start background spinner thread
        self._spinner_thread = threading.Thread(target=self._spin, daemon=True)
        self._spinner_thread.start()

        return self

    def __exit__(self, *args):
        """End Live display context."""
        self._stop_spinner.set()
        if self._spinner_thread:
            self._spinner_thread.join(timeout=0.5)
        if self._live:
            self._live.__exit__(*args)
        self._restore_terminal_mode(self._saved_termios)
        self._saved_termios = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_tool_call(self, tool_name, tool_result=None, command=None):
        """Add a tool call message to the panel and refresh display.

        Delegates formatting to core.tool_feedback.build_panel_tool_message
        to avoid duplicating display logic.

        Args:
            tool_name: Name of the tool (e.g., "read_file", "rg")
            tool_result: Optional tool result string (for detailed formatting)
            command: Optional command string for context
        """
        self.total_tool_calls += 1
        message = build_panel_tool_message(tool_name, tool_result, command)
        self.tool_calls.append(message)
        # Keep only last 5 tool calls
        if len(self.tool_calls) > 5:
            self.tool_calls.pop(0)
        self._live.update(self._render_panel())

    def append(self, text):
        """Append text to panel and refresh display (kept for compatibility).

        Args:
            text: Text to append (may contain Rich markup)
        """
        # Just update panel to refresh title counter
        self._live.update(self._render_panel())

    def set_complete(self, usage=None):
        """Mark panel as complete with optional token info.

        Args:
            usage: Optional dict with 'prompt', 'completion', 'total' token counts
        """
        self._show_spinner = False  # Stop spinner

        # Build title with token usage if available
        if usage and usage.get('total_tokens'):
            total_tokens = usage.get('total_tokens', 0)
            title = f"[green]✓ Sub-Agent Complete ({self.total_tool_calls}) - {total_tokens:,} tokens[/green]"
        else:
            title = f"[green]✓ Sub-Agent Complete ({self.total_tool_calls})[/green]"

        self._live.update(self._render_panel(
            title=title,
            border_style="green"
        ))

    def set_error(self, message):
        """Show error in panel with red styling.

        Args:
            message: Error message to display
        """
        self._show_spinner = False  # Stop spinner
        self._live.update(Panel(
            message,
            title="[red]✗ Sub-Agent Error[/red]",
            title_align="left",
            border_style="red",
            padding=(0, 1),
        ))
