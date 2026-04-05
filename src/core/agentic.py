"""Agent tool-calling loop."""

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text
from rich.live import Live

from utils.markdown import left_align_headings
from utils.settings import MAX_TOOL_CALLS, MAX_COMMAND_OUTPUT_LINES, MonokaiDarkBGStyle
from tools import (
    confirm_tool,
    read_file,
    list_directory,
    create_file,
    TOOLS,
    _tools_for_mode,
)
from utils.settings import tool_settings
from utils.result_parsers import (
    extract_exit_code,
    extract_all_metadata,
    extract_multiple_metadata,
)
from tools.task_list import _format_task_list
from exceptions import (
    LLMError,
    LLMConnectionError,
    LLMResponseError,
    CommandExecutionError,
    FileEditError,
)


def _vault_root_str() -> Optional[str]:
    """Return vault_root from the active VaultSession, or None."""
    try:
        from tools.obsidian import get_vault_session
        session = get_vault_session()
        return str(session.vault_root) if session else None
    except Exception:
        return None


def _print_or_append(text, console, panel_updater, markup=True):
    """Print text to console or append to panel_updater.

    Args:
        text: Text to display
        console: Rich console
        panel_updater: Optional SubAgentPanel for live updates
        markup: If True, parse Rich markup (only used for console)
    """
    if panel_updater:
        panel_updater.append(text)
    else:
        console.print(text, markup=markup)


# File extension to Pygments lexer name mapping for syntax highlighting
_LEXER_MAP = {
    'py': 'python',
    'js': 'javascript',
    'ts': 'typescript',
    'tsx': 'typescript',
    'jsx': 'javascript',
    'go': 'go',
    'rs': 'rust',
    'java': 'java',
    'c': 'c',
    'cpp': 'cpp',
    'h': 'c',
    'hpp': 'cpp',
    'sh': 'bash',
    'bash': 'bash',
    'zsh': 'bash',
    'yaml': 'yaml',
    'yml': 'yaml',
    'json': 'json',
    'toml': 'toml',
    'md': 'markdown',
    'html': 'html',
    'css': 'css',
    'sql': 'sql',
    'php': 'php',
    'rb': 'ruby',
    'swift': 'swift',
    'kt': 'kotlin',
    'scala': 'scala',
    'lua': 'lua',
    'r': 'r',
}


def _strip_leading_task_list_echo(content, task_list, title=None):
    """Remove a leading echoed task list from assistant content.

    Some models copy the task list tool output into the final response, which
    causes duplicate task list rendering in the CLI.
    """
    if not content or not isinstance(content, str) or not task_list:
        return content or ""

    expected = _format_task_list(task_list, title).strip()
    if not expected:
        return content

    trimmed = content.lstrip()
    if trimmed.startswith(expected):
        remainder = trimmed[len(expected):]
        return remainder.lstrip("\n").lstrip()

    return content


def _build_read_file_label(path, start_line=None, max_lines=None, with_colon=False):
    """Build uniform read_file label.

    Args:
        path: File path
        start_line: Optional starting line number (unused in display)
        max_lines: Optional max lines to read (unused in display)
        with_colon: If True, use 'read_file: path' format (for batch mode labels)

    Returns:
        str: Formatted label
    """
    separator = ': ' if with_colon else ' '
    label = f"read_file{separator}{path}"
    return label


def _parse_obsidian_resolve_feedback(tool_result, command=""):
    """Extract obsidian_resolve display summary from tool result.

    Returns:
        str: Display message like "3 backlinks" or "No backlinks found" or empty
    """
    lines = tool_result.split('\n')
    if any(re.search(r'Backlinks \((\d+)\)', line) for line in lines):
        for line in lines:
            m = re.search(r'Backlinks \((\d+)\)', line)
            if m:
                count = int(m.group(1))
                return f"{count} backlink{'s' if count != 1 else ''}"
    elif any("No backlinks found" in line for line in lines):
        return "No backlinks found"
    return ""


def _parse_obsidian_frontmatter_feedback(tool_result):
    """Extract obsidian_frontmatter display summary from tool result.

    Returns:
        tuple: (message, is_error) where message is the display string
    """
    lines = tool_result.split('\n')
    exit_code = extract_exit_code(tool_result)

    if exit_code == 1:
        return "Error reading note", True

    if any("No frontmatter found" in line for line in lines):
        return "No frontmatter found", False

    # Count metadata keys
    key_count = 0
    for line in lines[1:]:
        stripped = line.strip()
        if stripped and not stripped.startswith("Frontmatter for:") and not stripped.startswith("Body lines:"):
            key_count += 1

    if key_count > 0:
        body_lines = 0
        for line in lines:
            m = re.search(r'Body lines: (\d+)', line)
            if m:
                body_lines = int(m.group(1))
                break
        parts = [f"{key_count} field{'s' if key_count != 1 else ''}"]
        if body_lines > 0:
            parts.append(f"{body_lines} body lines")
        return ', '.join(parts), False

    return "", False


def _build_tool_label(function_name, arguments):
    """Build tool label with arguments for display.

    Args:
        function_name: Name of the tool function
        arguments: Dictionary of tool arguments

    Returns:
        str: Formatted label with arguments (e.g., "read_file: path/to/file", "rg: search_pattern")
    """
    if function_name == "rg":
        pattern = arguments.get('pattern', '')
        # Truncate long patterns for display
        return f"rg: {pattern[:40]}" if pattern else "rg"
    elif function_name == "read_file":
        path = arguments.get('path_str', '')
        return _build_read_file_label(path, with_colon=True)
    elif function_name == "list_directory":
        path = arguments.get('path_str', '')
        return f"list_directory: {path}"
    elif function_name == "create_file":
        path = arguments.get('path_str', '')
        return f"create_file: {path}"
    elif function_name == "edit_file":
        path = arguments.get('path', '')
        return f"edit_file: {path}"
    elif function_name == "web_search":
        query = arguments.get('query', '')
        return f"web search | {query}"
    elif function_name == "execute_command":
        command = arguments.get('command', '')
        # Truncate long commands for display
        return f"execute_command: {command[:80]}" if command else "execute_command"
    elif function_name == "obsidian_resolve":
        name = arguments.get('name', '')
        backlinks = arguments.get('get_backlinks', False)
        suffix = " (backlinks)" if backlinks else ""
        return f"obsidian_resolve: {name}{suffix}" if name else "obsidian_resolve"
    elif function_name == "obsidian_frontmatter":
        path = arguments.get('path_str', '')
        return f"obsidian_frontmatter: {path}" if path else "obsidian_frontmatter"
    else:
        return function_name


def _handle_create_file_feedback(tool_result, console, panel_updater):
    """Handle feedback for create_file tool.

    Display syntax-highlighted file preview.
    """
    lines = tool_result.split('\n')
    # Extract path from metadata
    path_match = re.search(r'path=([^\s]+)', tool_result)
    path_str = path_match.group(1) if path_match else "file"

    # Find file content section
    content_start = None
    content_end = None
    for i, line in enumerate(lines):
        if line.startswith("=== FILE_CONTENT ==="):
            content_start = i + 1
        elif line.startswith("=== END_FILE_CONTENT ===") and content_start is not None:
            content_end = i
            break

    # Display summary and syntax-highlighted content
    if content_start is not None and content_end is not None:
        content_lines = lines[content_start:content_end]
        content = "\n".join(content_lines)

        # Get file extension for syntax highlighting
        file_ext = Path(path_str).suffix[1:] if Path(path_str).suffix else "text"
        lexer_name = _LEXER_MAP.get(file_ext.lower(), 'text')

        # Create syntax object
        syntax = Syntax(
            content,
            lexer_name,
            theme=MonokaiDarkBGStyle,
            line_numbers=True,
            word_wrap=False
        )

        # Show with prefix for console
        if panel_updater:
            panel_updater.append(f"Created: {path_str}")
            panel_updater.append(str(syntax))
        else:
            console.print(f"Created: {path_str}", markup=False)
            console.print(syntax)
    else:
        # Fallback: just show path
        prefix = "╰─ " if not panel_updater else ""
        message = f"{prefix}Created: {path_str}"
        _print_or_append(message, console, panel_updater)

    if not panel_updater:
        console.print()


def _handle_list_directory_feedback(tool_result, console, panel_updater):
    """Handle feedback for list_directory tool.

    Display formatted directory tree with files and directories.
    """
    lines = tool_result.split('\n')
    # Extract items_count from metadata
    items_count = 0
    for line in lines:
        match = re.search(r'items_count=(\d+)', line)
        if match:
            items_count = int(match.group(1))
            break

    # Parse content lines (skip metadata lines)
    content_start = None
    for i, line in enumerate(lines):
        if line.startswith("FILE") or line.startswith("DIR"):
            content_start = i
            break

    if content_start is not None and items_count > 0:
        content_lines = lines[content_start:]

        # Parse entries: kind, path, line_count, size
        # Format: FILE  path/to/file.py       123 lines  12345 bytes
        #         DIR   path/to/dir/             0 lines
        entries = []
        for line in content_lines:
            # Use regex to extract parts - handles paths with spaces
            # Pattern: <KIND>  <path>  <line_count> lines  [<size> bytes]
            file_match = re.match(r'^(FILE|DIR)\s+(.+?)\s+(\d+)\s+lines(?:\s+(\d+)\s+bytes)?$', line)
            if file_match:
                kind = file_match.group(1)
                path = file_match.group(2).strip()
                line_count = file_match.group(3)
                size = file_match.group(4) if file_match.group(4) else None

                if kind == "FILE":
                    entries.append(("FILE", path, size if size else "?"))
                else:  # DIR
                    entries.append(("DIR", path))

        # Sort: directories first, then alphabetically
        entries.sort(key=lambda x: (0 if x[0] == "DIR" else 1, x[1]))

        # Build tree with truncation (max 10 items)
        max_display = 10
        display_entries = entries[:max_display]
        remaining = max(0, items_count - max_display)

        # Format tree lines
        tree_lines = []
        for i, entry in enumerate(display_entries):
            is_last = (i == len(display_entries) - 1) and (remaining == 0)
            # Use closing pipe (└─) for last item, otherwise middle pipe (├─)
            connector = "└─" if is_last else "├─"
            if entry[0] == "DIR":
                tree_lines.append(f"   {connector} {entry[1]}")
            else:  # FILE
                size_str = f"{int(entry[2]):,}" if entry[2].isdigit() else entry[2]
                tree_lines.append(f"   {connector} {entry[1]} ({size_str} bytes)")

        # Add overflow indicator if needed (always use closing pipe)
        if remaining > 0:
            tree_lines.append(f"   └─ ... and {remaining} more")

        # Build output with header
        path_match = re.search(r'path=([^\s]+)', tool_result)
        path_str = path_match.group(1) if path_match else "directory"
        header = f"{path_str}/ ({items_count} item{'s' if items_count != 1 else ''})"

        # Display with prefix
        prefix = "╰─ " if not panel_updater else ""
        output = f"{prefix}{header}\n"
        output += "\n".join(tree_lines)

        _print_or_append(output, console, panel_updater)

    if not panel_updater:
        console.print()


def _handle_execute_command_feedback(tool_result, console, panel_updater):
    """Handle feedback for execute_command tool.

    Display command output with line truncation and exit code.
    """
    lines = tool_result.split('\n')
    if lines:
        # Extract exit code from first line
        exit_code = extract_exit_code(tool_result)

        # Get output (all lines after the exit_code line)
        output_lines = lines[1:] if exit_code is not None else lines
        output_lines = [line for line in output_lines if line.strip()]

        # Truncate if too many lines
        truncation_message = None
        if len(output_lines) > MAX_COMMAND_OUTPUT_LINES:
            displayed_lines = output_lines[:MAX_COMMAND_OUTPUT_LINES]
            omitted = len(output_lines) - MAX_COMMAND_OUTPUT_LINES
            output = '\n'.join(displayed_lines)
            truncation_message = f"[dim]... ({omitted} more lines truncated)[/dim]"
        else:
            output = '\n'.join(output_lines)

        # Build prefix
        prefix = "╰─ " if not panel_updater else ""

        # Show output
        if output:
            display_text = f"{prefix}{output}"
            _print_or_append(display_text, console, panel_updater, markup=False)

        # Show truncation message separately to preserve markup
        if truncation_message:
            _print_or_append(truncation_message, console, panel_updater)

        # Show exit code if non-zero
        if exit_code is not None and exit_code != 0:
            exit_text = f"[dim](exit code: {exit_code})[/dim]"
            _print_or_append(exit_text, console, panel_updater)

    if not panel_updater:
        console.print()


def _display_tool_feedback(command, tool_result, console, indent=False, panel_updater=None):
    """Display user summary for read_file, rg, and list_directory.

    Args:
        command: Tool command string
        tool_result: Tool result string
        console: Rich console
        indent: If True, prefix with '│ ' (for sub-agent mode)
        panel_updater: Optional SubAgentPanel for live updates
    """
    if not tool_result:
        return

    # For sub-agent panel: add tool call with formatted message
    if panel_updater:
        # Extract tool name from command
        if command.startswith("read_file"):
            tool_name = "read_file"
        elif command.startswith("rg"):
            tool_name = "rg"
        elif command.startswith("list_directory"):
            tool_name = "list_directory"
        elif command.startswith(("create_task_list", "complete_task", "show_task_list")):
            tool_name = command.split()[0]
        elif command.startswith("web search"):
            tool_name = "web_search"
        elif command.startswith("execute_command"):
            tool_name = "execute_command"
        elif command.startswith("obsidian_resolve"):
            tool_name = "obsidian_resolve"
        elif command.startswith("obsidian_frontmatter"):
            tool_name = "obsidian_frontmatter"
        else:
            tool_name = command.split()[0]
        
        # Pass to panel updater which will handle formatting
        panel_updater.add_tool_call(tool_name, tool_result, command)

    # For task list tools: show the list (bounded by MAX_TASKS / MAX_TASK_LEN)
    if command.startswith(("create_task_list", "complete_task", "show_task_list")):
        exit_code = extract_exit_code(tool_result)
        if exit_code == 0 or exit_code is None:
            # Successful task list - display without exit_code line and without Rich markup parsing.
            rendered = tool_result
            if rendered.startswith("exit_code="):
                rendered = "\n".join(rendered.splitlines()[1:])
            _print_or_append(rendered.strip(), console, panel_updater, markup=False)
        else:
            # Show single-line error if present
            first_two = "\n".join(tool_result.splitlines()[:2]).strip()
            _print_or_append(first_two or tool_result.strip(), console, panel_updater, markup=False)
        if not panel_updater:
            console.print()
        return

    # For read_file: parse lines_read and start_line from first line
    if command.startswith("read_file"):
        metadata = extract_multiple_metadata(tool_result, 'lines_read', 'start_line')
        count = metadata.get('lines_read')
        if count is not None:
            # Only add prefix for console, not for panel_updater
            prefix = "╰─ " if not panel_updater else ""

            # Build message with line range if start_line is present
            start_line = metadata.get('start_line')
            if start_line:
                if start_line > 1:
                    end_line = start_line + count - 1
                    message = f"{prefix}[dim]Read lines {start_line}-{end_line} ({count} line{'s' if count != 1 else ''})[/dim]"
                else:
                    message = f"{prefix}[dim]Read {count} line{'s' if count != 1 else ''}[/dim]"
            else:
                message = f"{prefix}[dim]Read {count} line{'s' if count != 1 else ''}[/dim]"

            _print_or_append(message, console, panel_updater)
        if not panel_updater:
            console.print()
        return

    # For rg: parse matches/files from result
    if command.startswith("rg"):
        prefix = "╰─ " if not panel_updater else ""
        message = None

        # Check for "No matches found" message (0 results)
        lines = tool_result.split('\n')
        if any("No matches found" in line for line in lines):
            message = f"{prefix}[dim]No matches found[/dim]"
        # Check for matches=N or files=N pattern
        elif len(lines) > 1:
            metadata = extract_all_metadata(tool_result, line_index=1)
            count = metadata.get('matches') or metadata.get('files')
            if count is not None:
                label = 'matches' if 'matches' in metadata else 'files'
                if count == 0:
                    message = f"{prefix}[dim]No {label} found[/dim]"
                else:
                    message = f"{prefix}[dim]Found {count} {label}[/dim]"
        # Fallback: if exit_code=1 but no other info, show no matches
        elif any("exit_code=1" in line for line in lines):
            message = f"{prefix}[dim]No matches found[/dim]"

        if message:
            _print_or_append(message, console, panel_updater)
        if not panel_updater:
            console.print()
        return

    # For list_directory: parse and display directory tree
    if command.startswith("list_directory"):
        _handle_list_directory_feedback(tool_result, console, panel_updater)
        return

    # For create_file: display preview of created file
    if command.startswith("create_file"):
        _handle_create_file_feedback(tool_result, console, panel_updater)
        return

    # For execute_command: display command output with line truncation
    if command.startswith("execute_command"):
        _handle_execute_command_feedback(tool_result, console, panel_updater)
        return

    # For obsidian_resolve: display backlink count
    if command.startswith("obsidian_resolve"):
        prefix = "╰─ " if not panel_updater else ""
        msg = _parse_obsidian_resolve_feedback(tool_result, command)
        if msg:
            _print_or_append(f"{prefix}[dim]{msg}[/dim]", console, panel_updater)
        if not panel_updater:
            console.print()
        return

    # For obsidian_frontmatter: display parsed metadata summary
    if command.startswith("obsidian_frontmatter"):
        prefix = "╰─ " if not panel_updater else ""
        msg, is_error = _parse_obsidian_frontmatter_feedback(tool_result)
        if msg:
            _print_or_append(f"{prefix}[dim]{msg}[/dim]", console, panel_updater)
        if not panel_updater:
            console.print()
        return

    # For web_search: display results count
    if command.startswith("web search"):
        lines = tool_result.split('\n')
        if lines:
            # Extract results_found from first line
            match = re.search(r'results_found=(\d+)', lines[0])
            if match:
                count = int(match.group(1))
                # Only add prefix for console, not for panel_updater
                prefix = "╰─ " if not panel_updater else ""
                if count == 0:
                    message = f"{prefix}[dim]No results found[/dim]"
                else:
                    message = f"{prefix}[dim]Found {count} result{'s' if count != 1 else ''}[/dim]"
                _print_or_append(message, console, panel_updater)
        if not panel_updater:
            console.print()
        return


def _handle_empty_response(empty_response_count, console):
    """Handle empty response from model.

    Returns:
        tuple: (should_continue, updated_count)
    """
    empty_response_count += 1
    if empty_response_count >= 2:
        console.print("[red]Error: model returned empty response with no tool calls.[/red]")
        return False, empty_response_count
    return True, empty_response_count


# Timeout retry constants
_RETRY_MAX_ATTEMPTS = 3
_RETRY_DELAYS = (2, 4)  # exponential backoff per attempt
_RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
_RETRYABLE_ERROR_KEYWORDS = (
    "timeout", "timed out", "connectionerror", "connection refused",
    "connection reset", "connection aborted", "name or service not known",
    "network unreachable", "no route to host", "eof occurred",
)
_NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 405, 422}


def _is_retryable_error(error):
    """Check if an LLMConnectionError is retryable.

    Retryable conditions:
    - Timeout or connection-level errors (network unreachable, DNS failure, etc.)
    - HTTP 429 (rate limited), 502, 503, 504 (server errors)

    Non-retryable conditions:
    - HTTP 400, 401, 403, 405, 422 (client/auth errors)
    - LLMResponseError (malformed response data)

    Args:
        error: Exception instance (typically LLMConnectionError)

    Returns:
        bool: True if the error is retryable
    """
    # Never retry response parsing errors
    if isinstance(error, LLMResponseError):
        return False

    # Check HTTP status code first (most reliable signal)
    details = getattr(error, 'details', {}) or {}
    status_code = details.get("status_code")
    if status_code is not None:
        if status_code in _NON_RETRYABLE_STATUS_CODES:
            return False
        if status_code in _RETRYABLE_STATUS_CODES:
            return True

    # For network-level errors, check the original error message
    original_error = details.get("original_error", "")
    original_lower = original_error.lower()
    return any(keyword in original_lower for keyword in _RETRYABLE_ERROR_KEYWORDS)


def _wait_with_cancel_message(console, delay_seconds):
    """Wait briefly before retrying, showing a dim status line.

    Args:
        console: Rich console for output
        delay_seconds: Seconds to wait

    Returns:
        bool: True if wait completed, False if interrupted by KeyboardInterrupt
    """
    console.print(f"[dim]Connection issue, retrying in {delay_seconds}s... (Ctrl+C to cancel)[/dim]")
    try:
        time.sleep(delay_seconds)
    except KeyboardInterrupt:
        console.print("[dim]Retry cancelled.[/dim]")
        return False
    return True


def _handle_tool_limit_reached(chat_manager, console):
    """Handle case when tool call limit is exceeded.

    Returns:
        bool: True if handled successfully, False if error
    """
    chat_manager.messages.append({
        "role": "user",
        "content": "Tool limit reached. Provide your answer without calling tools."
    })

    try:
        response = chat_manager.client.chat_completion(
            chat_manager.messages, stream=False, tools=None
        )
    except LLMError as e:
        console.print(f"[red]LLM Error: {e}[/red]")
        return False

    if isinstance(response, dict) and 'usage' in response:
        chat_manager.token_tracker.add_usage(response['usage'])

    try:
        final_message = response["choices"][0]["message"]
    except (KeyError, IndexError):
        console.print("[red]Error: invalid response from model[/red]")
        return False

    content = final_message.get("content", "").strip()
    if content:
        md = Markdown(left_align_headings(content), code_theme=MonokaiDarkBGStyle, justify="left")
        console.print(md)
        chat_manager.messages.append(final_message)
        console.print()
        return True

    console.print("[red]Error: model returned empty response after tool limit reached.[/red]")
    return False


class SubAgentPanel:
    """Live panel for streaming sub-agent tool output."""

    # Spinner frames for animation
    _SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, query, console):
        """Initialize the sub-agent panel.

        Args:
            query: The task query for the sub-agent
            console: Rich console for display
        """
        self.console = console
        self.query = query
        self.tool_calls = []  # List of tool_name strings
        self.total_tool_calls = 0
        self._live = None
        self._spinner_index = 0
        self._show_spinner = True
        self._spinner_thread = None
        self._stop_spinner = threading.Event()

    def _get_title(self):
        """Get panel title with optional spinner and tool call counter.

        Returns:
            Rich markup string for the panel title
        """
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

    def _spin(self):
        """Background thread: continuously increment spinner and update display."""
        while not self._stop_spinner.is_set():
            self._spinner_index += 1
            if self._live:
                self._live.update(self._render_panel())
            time.sleep(0.1)  # 10 updates per second = smooth animation

    def __enter__(self):
        """Start Live display context.

        Returns:
            self for use in with statement
        """
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

    def add_tool_call(self, tool_name, tool_result=None, command=None):
        """Add a tool call message to the panel and refresh display.

        Args:
            tool_name: Name of the tool (e.g., "read_file", "rg")
            tool_result: Optional tool result string (for detailed formatting)
            command: Optional command string for context
        """
        # If no tool_result provided, just show tool name (backward compatibility)
        if tool_result is None:
            message = f"[grey]{tool_name}[/grey]"
            self.total_tool_calls += 1
            self.tool_calls.append(message)
            if len(self.tool_calls) > 5:
                self.tool_calls.pop(0)
            self._live.update(self._render_panel())
            return

        # Full implementation with tool_result
        self.total_tool_calls += 1
        
        # Format message based on tool type, matching main agent styling
        if tool_name == "read_file":
            # Extract path from command if available
            path = ""
            if command:
                # Command format is "read_file: path" (parallel) or "read_file path" (sequential)
                match = re.search(r'read_file:?\s+(.+)', command)
                if match:
                    path = match.group(1).strip()

            # Parse lines_read from result
            metadata = extract_multiple_metadata(tool_result, 'lines_read', 'start_line')
            count = metadata.get('lines_read')

            if count is not None:
                start_line = metadata.get('start_line')
                if start_line:
                    if start_line > 1:
                        end_line = start_line + count - 1
                        message = f"[grey]read_file {path}[/grey]\n[dim]╰─ Read lines {start_line}-{end_line} ({count} line{'s' if count != 1 else ''})[/dim]"
                    else:
                        message = f"[grey]read_file {path}[/grey]\n[dim]╰─ Read {count} line{'s' if count != 1 else ''}[/dim]"
                else:
                    message = f"[grey]read_file {path}[/grey]\n[dim]╰─ Read {count} line{'s' if count != 1 else ''}[/dim]"
            else:
                message = f"[grey]read_file {path}[/grey]"
        
        elif tool_name == "rg":
            # Extract pattern from command if available
            pattern = ""
            if command:
                # Command format is "rg: pattern" (parallel) or "rg pattern" (sequential)
                match = re.search(r'rg:?\s+(.+)', command)
                if match:
                    pattern = match.group(1).strip()

            # Parse matches/files from result
            lines = tool_result.split('\n')
            if len(lines) > 1:
                metadata = extract_all_metadata(tool_result, line_index=1)
                count = metadata.get('matches') or metadata.get('files')
                if count is not None:
                    label = 'matches' if 'matches' in metadata else 'files'
                    if count == 0:
                        message = f"[grey]rg {pattern}[/grey]\n[dim]╰─ No {label} found[/dim]"
                    else:
                        message = f"[grey]rg {pattern}[/grey]\n[dim]╰─ Found {count} {label}[/dim]"
                else:
                    message = f"[grey]rg {pattern}[/grey]"
            elif any("exit_code=1" in line for line in lines):
                # Fallback for no matches with minimal output
                message = f"[grey]rg {pattern}[/grey]\n[dim]╰─ No matches found[/dim]"
            else:
                message = f"[grey]rg {pattern}[/grey]"
            
            # Fallback: if exit_code=1 but no footer, add no matches
            if tool_result and "exit_code=1" in tool_result and "╰─" not in message:
                message = f"[grey]rg {pattern}[/grey]\n[dim]╰─ No matches found[/dim]"
        
        elif tool_name == "list_directory":
            # Extract path from command if available
            path = "."
            if command:
                # Command format is "list_directory: path" (parallel) or "list_directory path" (sequential)
                match = re.search(r'list_directory:?\s+(.+)', command)
                if match:
                    path = match.group(1).strip()
            
            # Parse items_count from result
            lines = tool_result.split('\n')
            items_count = 0
            for line in lines:
                match = re.search(r'items_count=(\d+)', line)
                if match:
                    items_count = int(match.group(1))
                    break
            
            if items_count > 0:
                message = f"[grey]list_directory {path}[/grey]\n[dim]╰─ {items_count} item{'s' if items_count != 1 else ''}[/dim]"
            else:
                message = f"[grey]list_directory {path}[/grey]\n[dim]╰─ No items[/dim]"
        
        elif tool_name == "web_search":
            # Extract query from command if available
            query = ""
            if command:
                # Command format is "web search | query"
                if "|" in command:
                    parts = command.split(" | ", 1)
                    if len(parts) > 1:
                        query = parts[1]

            # Parse results_found from tool_result
            lines = tool_result.split('\n')
            results_count = None
            if lines:
                match = re.search(r'results_found=(\d+)', lines[0])
                if match:
                    results_count = int(match.group(1))

            if query:
                if results_count is not None:
                    if results_count == 0:
                        message = f"[bold cyan]web search | {query}[/bold cyan]\n[dim]╰─ No results found[/dim]"
                    else:
                        message = f"[bold cyan]web search | {query}[/bold cyan]\n[dim]╰─ Found {results_count} result{'s' if results_count != 1 else ''}[/dim]"
                else:
                    message = f"[bold cyan]web search | {query}[/bold cyan]\n[dim]╰─ Search completed[/dim]"
            else:
                message = f"[bold cyan]web_search[/bold cyan]\n[dim]╰─ Search completed[/dim]"
        
        elif tool_name == "execute_command":
            # Extract command from the command parameter if available
            cmd_display = ""
            if command:
                # Command format is "execute_command: cmd" or just the cmd itself
                if command.startswith("execute_command"):
                    parts = command.split(' ', 1)
                    if len(parts) > 1:
                        cmd_display = parts[1]
                else:
                    # Just the command itself (from label builder)
                    cmd_display = command
            if cmd_display:
                message = f"[grey]{cmd_display}[/grey]\n[dim]╰─ Command executed[/dim]"
            else:
                message = f"[grey]execute_command[/grey]\n[dim]╰─ Command executed[/dim]"
        
        elif tool_name == "obsidian_resolve":
            name = ""
            if command:
                match = re.search(r'obsidian_resolve:?\s+(.+?)(?:\s+\(backlinks\))?$', command)
                if match:
                    name = match.group(1).strip()
            msg = _parse_obsidian_resolve_feedback(tool_result, command)
            if msg:
                message = f"[grey]obsidian_resolve {name}[/grey]\n[dim]╰─ {msg}[/dim]"
            else:
                message = f"[grey]obsidian_resolve {name}[/grey]"

        elif tool_name == "obsidian_frontmatter":
            path = ""
            if command:
                match = re.search(r'obsidian_frontmatter:?\s+(.+)', command)
                if match:
                    path = match.group(1).strip()
            msg, _ = _parse_obsidian_frontmatter_feedback(tool_result)
            if msg:
                message = f"[grey]obsidian_frontmatter {path}[/grey]\n[dim]╰─ {msg}[/dim]"
            else:
                message = f"[grey]obsidian_frontmatter {path}[/grey]"

        elif tool_name in ("create_task_list", "complete_task", "show_task_list"):
            # Handle task list tools - show the task list content
            exit_code = extract_exit_code(tool_result)
            if exit_code == 0 or exit_code is None:
                rendered = tool_result
                if rendered.startswith("exit_code="):
                    rendered = "\n".join(rendered.splitlines()[1:])
                message = f"[grey]{tool_name}[/grey]\n[dim]╰─ {rendered.strip()}[/dim]"
            else:
                first_two = "\n".join(tool_result.splitlines()[:2]).strip()
                message = f"[grey]{tool_name}[/grey]\n[dim]╰─ {first_two or tool_result.strip()}[/dim]"
        
        else:
            # Generic fallback
            message = f"[grey]{tool_name}[/grey]"
        
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
        # For now, just update panel to refresh title counter
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

        # Update panel with green complete title showing total tool calls and tokens
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


class AgenticOrchestrator:
    """Orchestrates the agentic tool-calling loop.

    This class encapsulates the complex logic of coordinating LLM interactions
    with tool calling, providing a cleaner, more maintainable structure.
    """

    def __init__(self, chat_manager, repo_root, rg_exe_path, console, debug_mode, suppress_result_display=False, is_sub_agent=False, panel_updater=None, force_parallel_execution=False):
        """Initialize the orchestrator.

        Args:
            chat_manager: ChatManager instance for state management
            repo_root: Path to repository root
            rg_exe_path: Path to rg.exe
            console: Rich console for output
            debug_mode: Whether to show debug output
            suppress_result_display: If True, suppress final LLM response display (for research agent)
            is_sub_agent: If True, running as sub-agent (for visual framing)
            panel_updater: Optional SubAgentPanel callback for live panel updates
            force_parallel_execution: If True, force parallel execution (for sub-agent)
        """
        self.chat_manager = chat_manager
        self.repo_root = repo_root
        self.rg_exe_path = rg_exe_path
        self.console = console
        self.debug_mode = debug_mode
        self.suppress_result_display = suppress_result_display
        self.is_sub_agent = is_sub_agent
        self.panel_updater = panel_updater
        self.force_parallel_execution = force_parallel_execution
        self.tool_calls_count = 0
        self.empty_response_count = 0
        self.gitignore_spec = chat_manager.get_gitignore_spec(repo_root)
        # For parallel execution: temporary console override
        self._parallel_context = {}
        # Initialize vault session with known repo_root (for project folder derivation)
        try:
            from tools.obsidian import init_session
            init_session(repo_root)
        except Exception as e:
            logger.warning("Failed to initialize vault session: %s", e)


    def _get_console(self):
        """Get the console for output, respecting parallel execution context.

        Returns:
            Console object or None if suppressed during parallel execution
        """
        # Check if we're in a parallel context with suppressed console
        return self._parallel_context.get('console', self.console)

    def run(self, user_input, thinking_indicator=None, allowed_tools=None):
        """Main orchestration loop.

        Args:
            user_input: User's input message
            thinking_indicator: Optional ThinkingIndicator instance
            allowed_tools: Optional list of allowed tool names (for research)
        """
        # Append user message
        self.chat_manager.messages.append({"role": "user", "content": user_input})

        # Log user message
        self.chat_manager.log_message({"role": "user", "content": user_input})

        while True:
            # Get response from LLM
            response = self._get_llm_response(allowed_tools=allowed_tools)
            if response is None:
                return

            # Auto-compact if over token threshold (applies to both main agent and subagent)
            self.chat_manager.maybe_auto_compact()

            # Check for tool calls
            tool_calls = response.get("tool_calls")

            if not tool_calls:
                if self._handle_final_response(response, thinking_indicator):
                    return
            else:
                should_exit = self._handle_tool_calls(response, thinking_indicator, allowed_tools)
                if should_exit:
                    return

    def _get_llm_response(self, allowed_tools=None):
        """Get next LLM response with tool definitions.

        Includes automatic retry with live countdown for timeout/connection errors.
        Retries up to 3 times with a 5-second countdown between attempts.

        Args:
            allowed_tools: Optional list of allowed tool names (overrides mode-based filtering)

        Returns:
            Response dict from LLM, or None if error occurred
        """
        # Use allowed_tools if provided, otherwise use mode-based filtering
        if allowed_tools is not None:
            # Validate that allowed_tools is not empty
            if not allowed_tools:
                self.console.print("[red]Error: allowed_tools is empty[/red]")
                return None
            # TOOLS is a function, call it to get the list
            tools = [tool for tool in TOOLS() if tool["function"]["name"] in allowed_tools]
            # Log filtered tools for debugging
            if self.debug_mode:
                tool_names = [t["function"]["name"] for t in tools]
                self.console.print(f"[dim]Available tools: {tool_names}[/dim]")
        else:
            tools = _tools_for_mode(self.chat_manager.interaction_mode)

        # Retry loop for timeout/connection errors
        last_error = None
        for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
            try:
                response = self.chat_manager.client.chat_completion(
                    self.chat_manager.messages, stream=False, tools=tools
                )
            except LLMError as e:
                last_error = e

                # Check if this error is retryable
                if _is_retryable_error(e) and attempt < _RETRY_MAX_ATTEMPTS:
                    delay = _RETRY_DELAYS[min(attempt - 1, len(_RETRY_DELAYS) - 1)]
                    wait_ok = _wait_with_cancel_message(self.console, delay)
                    if not wait_ok:
                        return None
                    continue
                else:
                    # Non-retryable error or final attempt exhausted
                    self.console.print(f"[red]LLM Error: {e}[/red]")
                    return None

            # Successful response — parse and return
            # Extract and track usage data
            if isinstance(response, dict) and 'usage' in response:
                self.chat_manager.token_tracker.add_usage(response['usage'])

            try:
                message = response["choices"][0]["message"]
            except (KeyError, IndexError):
                self.console.print("[red]Error: invalid response from model[/red]")
                return None

            return message

        # Should not reach here, but handle gracefully
        self.console.print(f"[red]LLM Error: {last_error}[/red]")
        return None

    def _handle_final_response(self, response, thinking_indicator=None):
        """Handle non-tool-call response (final answer).

        Args:
            response: Message dict from LLM
            thinking_indicator: Optional ThinkingIndicator instance to clear before displaying

        Returns:
            True if handled successfully, False if should continue looping
        """
        content = response.get("content", "")
        content = _strip_leading_task_list_echo(
            content,
            getattr(self.chat_manager, "task_list", None) or [],
            getattr(self.chat_manager, "task_list_title", None),
        )
        # Strip leading "Assistant: " prefix that some models may output
        content = content.lstrip("Assistant: ").lstrip()
        if content and content.strip():
            # Clear thinking indicator before printing response to avoid flash
            if thinking_indicator:
                thinking_indicator.stop(reset=True)
            # Only display to user if result display is not suppressed
            if not self.suppress_result_display:
                md = Markdown(left_align_headings(content), code_theme=MonokaiDarkBGStyle, justify="left")
                self.console.print(md)
            # Always append to message history (AI needs the result regardless)
            response = dict(response)
            response["content"] = content
            self.chat_manager.messages.append(response)
            # Log assistant response
            self.chat_manager.log_message(response)

            # NEW: Compact tool results after final answer (per-message compaction)
            self.chat_manager.compact_tool_results()

            # Update context tokens with current mode's tools
            tools_for_mode = _tools_for_mode(self.chat_manager.interaction_mode)
            self.chat_manager._update_context_tokens(tools_for_mode)

            self.console.print()
            return True

        # Empty response with no tools
        should_continue, self.empty_response_count = _handle_empty_response(
            self.empty_response_count, self.console
        )
        return not should_continue

    def _handle_tool_calls(self, response, thinking_indicator, allowed_tools=None):
        """Process tool calls and display accompanying content.

        Args:
            response: Full message dict from LLM (includes content and tool_calls)
            thinking_indicator: Optional ThinkingIndicator instance
            allowed_tools: Optional list of allowed tool names

        Returns:
            True if should exit the orchestration loop
        """
        # Extract tool_calls from response
        tool_calls = response.get("tool_calls")
        if not tool_calls:
            return False  # Should not happen if called correctly

        # Append assistant message with ALL tool calls (include content if present)
        # This must happen BEFORE filtering so the LLM sees its original intent
        content = (response.get("content") or "").strip()
        assistant_msg = {"role": "assistant", "tool_calls": tool_calls}
        if content:
            assistant_msg["content"] = content
        self.chat_manager.messages.append(assistant_msg)
        # Log assistant tool call message
        self.chat_manager.log_message(assistant_msg)

        # NEW: Filter out non-allowed tools BEFORE execution
        # This silently removes unknown tools or tools not in the allowed whitelist
        # to prevent error messages from reaching the user while allowing the agent
        # to continue with alternative tools.
        from tools.base import ToolRegistry

        filtered_calls = []
        filtered_tool_ids = []  # Track filtered tool IDs to provide feedback

        for tool_call in tool_calls:
            function_name = tool_call.get("function", {}).get("name")

            # Check if tool exists in registry
            if not ToolRegistry.get(function_name):
                # Silent fail - skip this tool call entirely
                # Agent will receive empty result and can retry with correct tool
                if self.debug_mode:
                    self.console.print(f"[dim]Silently filtered unknown tool: {function_name}[/dim]")
                filtered_tool_ids.append(tool_call.get("id"))
                continue

            # Check if tool is in allowed_tools whitelist (if provided)
            if allowed_tools and function_name not in allowed_tools:
                # Silent fail - skip this tool
                if self.debug_mode:
                    self.console.print(f"[dim]Silently filtered non-allowed tool: {function_name}[/dim]")
                filtered_tool_ids.append(tool_call.get("id"))
                continue

            filtered_calls.append(tool_call)

        # Replace with filtered list
        tool_calls = filtered_calls

        # Provide feedback to agent for filtered tools
        # This allows the agent to understand which tools were not available
        # without showing error messages to the user
        if filtered_tool_ids:
            for tool_id in filtered_tool_ids:
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": "exit_code=1\nTool not available. Please use the available tools from the function list."
                }
                self.chat_manager.messages.append(tool_msg)
                self.chat_manager.log_message(tool_msg)

        # If all tools were filtered, return early
        if not tool_calls:
            if self.debug_mode:
                self.console.print("[dim]All tool calls were filtered, continuing...[/dim]")
            return False

        self.empty_response_count = 0
        self.tool_calls_count += 1

        if self.tool_calls_count > MAX_TOOL_CALLS:
            return not _handle_tool_limit_reached(self.chat_manager, self.console)

        # Display conversational content if present
        # Skip if calling sub_agent OR if we ARE a sub-agent (sub-agent panel provides context)
        is_calling_sub_agent = any(
            tool.get("function", {}).get("name") == "sub_agent"
            for tool in tool_calls
        )
        # Route to panel if we're a sub-agent with a panel_updater, otherwise print to console
        if content:
            if self.is_sub_agent and self.panel_updater:
                # Sub-agent: send thinking to panel instead of console
                self.panel_updater.append(content)
            elif not is_calling_sub_agent:
                # Main agent: print to console (unless calling sub_agent)
                md = Markdown(left_align_headings(content), code_theme=MonokaiDarkBGStyle, justify="left")
                self.console.print(md)
                self.console.print()

        # Check if we should use parallel execution
        from utils.settings import tool_settings
        use_parallel = (
            tool_settings.enable_parallel_execution and
            len(tool_calls) > 1 and
            (self.chat_manager.interaction_mode != "plan" or self.force_parallel_execution)  # Sequential in plan mode unless forced
        )

        # Force sequential if any edit_file or execute_command in the batch (safety)
        if use_parallel:
            for tool_call in tool_calls:
                tool_name = tool_call.get("function", {}).get("name")
                if tool_name == "edit_file":
                    use_parallel = False
                    if self.debug_mode:
                        self.console.print("[dim]Forcing sequential execution (edit_file detected)[/dim]")
                    break
                elif tool_name == "execute_command":
                    use_parallel = False
                    if self.debug_mode:
                        self.console.print("[dim]Forcing sequential execution (execute_command detected)[/dim]")
                    break
                elif tool_name == "sub_agent":
                    use_parallel = False
                    if self.debug_mode:
                        self.console.print("[dim]Forcing sequential execution (sub_agent detected)[/dim]")
                    break

        if use_parallel and self.debug_mode:
            self.console.print(f"[cyan]Executing {len(tool_calls)} tools in parallel[/cyan]")

        # Lock compaction during tool execution to prevent orphaning tool_call_ids
        self.chat_manager._compaction_locked = True

        if use_parallel:
            result = self._execute_tools_parallel(response, thinking_indicator)
        else:
            result = self._execute_tools_sequential(tool_calls, thinking_indicator)

        # Unlock compaction after all tool results are appended
        self.chat_manager._compaction_locked = False

        return result

    def _execute_tools_sequential(self, tool_calls, thinking_indicator):
        """Execute tools one at a time (original behavior).

        Args:
            tool_calls: List of tool call dicts from LLM
            thinking_indicator: Optional ThinkingIndicator instance

        Returns:
            True if should exit the orchestration loop
        """
        end_loop = False

        for tool_call in tool_calls:
            tool_id = tool_call["id"]
            function_name = tool_call["function"]["name"]

            should_exit, tool_result = self._process_single_tool_call(
                tool_call, thinking_indicator
            )

            if should_exit:
                # Cancel was selected - append this result and break immediately
                if tool_result is not None and tool_result is not False:
                    from rich.text import Text
                    if isinstance(tool_result, Text):
                        content_for_agent = f"exit_code=0\n{str(tool_result)}"
                    else:
                        content_for_agent = str(tool_result)
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": content_for_agent
                    }
                    self.chat_manager.messages.append(tool_msg)
                    self.chat_manager.log_message(tool_msg)
                return True  # Exit orchestration loop immediately

            # Append tool result if not skipped (guidance mode)
            if tool_result is not None and tool_result is not False:
                from rich.text import Text
                # Add exit_code prefix for agent consumption
                if isinstance(tool_result, Text):
                    # Rich Text object = successful edit (exit_code=0)
                    content_for_agent = f"exit_code=0\n{str(tool_result)}"
                else:
                    content_for_agent = str(tool_result)
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": content_for_agent
                }
                self.chat_manager.messages.append(tool_msg)
                # Log tool result
                self.chat_manager.log_message(tool_msg)

        # Update context tokens with current mode's tools
        tools_for_mode = _tools_for_mode(self.chat_manager.interaction_mode)
        self.chat_manager._update_context_tokens(tools_for_mode)

        return end_loop

    def _execute_tools_parallel(self, response, thinking_indicator):
        """Execute multiple tools concurrently.

        Args:
            response: Full message dict from LLM (includes content and tool_calls)
            thinking_indicator: Optional ThinkingIndicator instance

        Returns:
            True if should exit the orchestration loop
        """
        # Extract tool_calls from response
        tool_calls = response.get("tool_calls")
        if not tool_calls:
            return False
        from tools.parallel_executor import ParallelToolExecutor, ToolCall

        # Suppress console output in handlers during parallel execution
        # We'll display results ourselves in order below
        self._parallel_context['console'] = None

        try:
            # Prepare context
            context = {
                'thinking_indicator': thinking_indicator,
                'repo_root': self.repo_root,
                'chat_manager': self.chat_manager,
                'rg_exe_path': self.rg_exe_path,
                'debug_mode': self.debug_mode,
                'gitignore_spec': self.gitignore_spec,
                'panel_updater': self.panel_updater,
                'interaction_mode': self.chat_manager.interaction_mode,
                'vault_root': _vault_root_str(),
            }

            # Convert to ToolCall objects
            tool_call_objs = []
            for i, tc in enumerate(tool_calls):
                try:
                    arguments = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    # Invalid JSON - handle inline for this tool
                    self.chat_manager.log_message({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": "exit_code=1\nInvalid JSON arguments"
                    })
                    continue

                tool_call_objs.append(
                    ToolCall(
                        tool_id=tc["id"],
                        function_name=tc["function"]["name"],
                        arguments=arguments,
                        call_index=i
                    )
                )

            if not tool_call_objs:
                # All tools had invalid arguments
                return False

            # Create executor
            from utils.settings import tool_settings
            executor = ParallelToolExecutor(
                max_workers=tool_settings.max_parallel_workers
            )

            # Execute in parallel
            results, had_errors = executor.execute_tools(
                tool_call_objs,
                context
            )

            # Display results with labels (staggered: label → feedback, like sequential mode)
            for result in results:
                if result.success:
                    # Get tool call info
                    tool_call = tool_calls[result.call_index]
                    function_name = tool_call.get("function", {}).get("name", "")
                    arguments = tool_call.get("function", {}).get("arguments", "{}")
                    args_dict = json.loads(arguments) if isinstance(arguments, str) else arguments

                    # Label builders
                    label_builders = {
                        "rg": lambda a: f"rg: {a.get('pattern', '')[:40]}",
                        "read_file": lambda a: _build_read_file_label(
                            a.get('path_str', ''),
                            a.get('start_line'),
                            a.get('max_lines'),
                            with_colon=True
                        ),
                        "list_directory": lambda a: f"list_directory: {a.get('path_str', '')}",
                        "create_file": lambda a: f"create_file: {a.get('path_str', '')}",
                        "web_search": lambda a: f"web search | {a.get('query', '')}",
                        "create_task_list": lambda a: "create_task_list",
                        "complete_task": lambda a: "complete_task",
                        "show_task_list": lambda a: "show_task_list",
                        "obsidian_resolve": lambda a: f"obsidian_resolve: {a.get('name', '')}" + (" (backlinks)" if a.get('get_backlinks') else ""),
                        "obsidian_frontmatter": lambda a: f"obsidian_frontmatter: {a.get('path_str', '')}",
                    }

                    # Print the label first
                    label_builder = label_builders.get(function_name, lambda a: function_name)
                    try:
                        label = label_builder(args_dict)
                        if function_name == "web_search":
                            label_text = f"[bold cyan]{label}[/bold cyan]"
                        else:
                            label_text = f"[grey]{label}[/grey]"
                        
                        # Route to panel_updater for sub-agent, otherwise console
                        # For panel_updater, _display_tool_feedback will handle the complete display
                        if not self.panel_updater:
                            self.console.print(label_text, highlight=False)
                            # Force flush to ensure label appears immediately
                            self.console.file.flush()
                    except Exception:
                        label_text = f"[grey]{function_name}[/grey]"
                        if not self.panel_updater:
                            self.console.print(label_text, highlight=False)
                            self.console.file.flush()

                    # Display feedback immediately after label (no buffering)
                    try:
                        if function_name == "edit_file" and result.requires_approval:
                            # Handle approval workflow for edit_file in parallel mode
                            from rich.text import Text

                            thinking_indicator = context.get('thinking_indicator')

                            if isinstance(result.result, Text):
                                # Rich Text object (new format with styling)
                                approved_result, should_exit = self._handle_edit_approval(
                                    result.result, args_dict.get('path', ''), args_dict,
                                    self.console, thinking_indicator)
                                result.result = approved_result
                                if should_exit:
                                    result.should_exit = True
                            elif result.result.startswith("exit_code=0"):
                                # Legacy string format - parse and display
                                lines = result.result.split('\n')
                                preview_lines = [line for line in lines if not line.startswith("exit_code=")]
                                preview = '\n'.join(preview_lines).strip()

                                approved_result, should_exit = self._handle_edit_approval(
                                    preview, args_dict.get('path', ''), args_dict,
                                    self.console, thinking_indicator)
                                result.result = approved_result
                                if should_exit:
                                    result.should_exit = True
                            else:
                                # Error occurred during preview - don't show to user, but still return to agent
                                pass
                        elif label:
                            _display_tool_feedback(label, result.result, self.console, panel_updater=self.panel_updater)
                            # Force flush to ensure immediate output
                            if not self.panel_updater:
                                self.console.file.flush()
                        else:
                            completion_text = f"[dim]{function_name} completed[/dim]"
                            if self.panel_updater:
                                self.panel_updater.append(completion_text)
                            else:
                                self.console.print(completion_text, highlight=False)
                                self.console.file.flush()
                    except Exception:
                        completion_text = f"[dim]{function_name} completed[/dim]"
                        if self.panel_updater:
                            self.panel_updater.append(completion_text)
                        else:
                            self.console.print(completion_text, highlight=False)
                            self.console.file.flush()
                else:
                    error_msg = result.error or result.result
                    error_text = f"[red]{error_msg}[/red]"
                    if self.panel_updater:
                        self.panel_updater.append(error_text)
                    else:
                        self.console.print(error_text, markup=False)
                        self.console.file.flush()

            # Display summary
            success_count = sum(1 for r in results if r.success)
            if self.debug_mode:
                self.console.print(
                    f"[dim]Parallel execution: {success_count}/{len(results)} succeeded[/dim]"
                )

            # Append all results to chat history
            end_loop = False
            for result in results:
                if result.success:
                    # Check if tool requested exit
                    if result.should_exit:
                        end_loop = True

                    # Add exit_code prefix for agent consumption (Rich Text = success)
                    from rich.text import Text
                    if isinstance(result.result, Text):
                        content_for_agent = f"exit_code=0\n{str(result.result)}"
                    else:
                        content_for_agent = str(result.result)
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": result.tool_id,
                        "content": content_for_agent
                    }
                    self.chat_manager.messages.append(tool_msg)
                    # Log tool result
                    self.chat_manager.log_message(tool_msg)
                else:
                    # Tool failed
                    error_msg = result.error or result.result
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": result.tool_id,
                        "content": f"exit_code=1\n{error_msg}"
                    }
                    self.chat_manager.messages.append(tool_msg)
                    # Log tool result
                    self.chat_manager.log_message(tool_msg)

            # Update context tokens with current mode's tools
            tools_for_mode = _tools_for_mode(self.chat_manager.interaction_mode)
            self.chat_manager._update_context_tokens(tools_for_mode)

            return end_loop
        finally:
            # Restore console output
            self._parallel_context['console'] = self.console

    def _handle_edit_approval(self, preview, file_path, args_dict, console, thinking_indicator):
        """Handle edit_file approval workflow (shared between sequential and parallel paths).

        Args:
            preview: Either a rich Text object or a plain string to display.
            file_path: The file path being edited (for the confirm prompt).
            args_dict: Tool arguments dict (path, search, replace, context_lines).
            console: Rich console for display.
            thinking_indicator: ThinkingIndicator instance (may be None).

        Returns:
            (result_str, should_exit) tuple where should_exit=True means cancel the agentic loop.
        """
        # Display preview
        console.print(preview)
        console.print()

        # Stop thinking indicator while waiting for user input
        if thinking_indicator:
            thinking_indicator.stop()

        action, guidance = confirm_tool(
            f"edit_file: {file_path}",
            console,
            reason=args_dict.get('reason', 'Apply file edit with above changes'),
            requires_approval=True,
            approve_mode=self.chat_manager.approve_mode,
            is_edit_tool=True,
            cycle_approve_mode=lambda: self.chat_manager.cycle_approve_mode()
        )

        if action == "accept":
            from tools.edit import _execute_edit_file
            final_result = _execute_edit_file(
                path=args_dict.get('path'),
                search=args_dict.get('search'),
                replace=args_dict.get('replace'),
                repo_root=self.repo_root,
                console=console,
                gitignore_spec=self.gitignore_spec,
                context_lines=args_dict.get('context_lines', 3),
                vault_root=_vault_root_str()
            )
            # Strip exit_code line from final result before displaying
            if final_result and isinstance(final_result, str):
                result_lines = [line for line in final_result.split('\n') if not line.startswith('exit_code=')]
                final_result = '\n'.join(result_lines).strip()
            result_str, should_exit = final_result, False
        elif action == "advise":
            console.print(f"[dim]Edit not applied. User advice: {guidance}[/dim]")
            result_str = f"exit_code=1\nEdit not applied. User advice: {guidance}"
            should_exit = False
        else:  # cancel
            console.print("[dim]Operation canceled by user.[/dim]")
            result_str = "exit_code=1\nOperation canceled by user. Do not retry this operation."
            should_exit = True

        # Restart thinking indicator after user input
        if thinking_indicator:
            thinking_indicator.start()

        return result_str, should_exit

    def _process_single_tool_call(self, tool_call, thinking_indicator):
        """Process a single tool call.

        Args:
            tool_call: Tool call dict from LLM
            thinking_indicator: Optional ThinkingIndicator instance

        Returns:
            Tuple of (should_exit, tool_result)
            - should_exit: True if should exit orchestration loop
            - tool_result: Result string, or None if already appended, False if skipped
        """
        tool_id = tool_call["id"]
        function_name = tool_call["function"]["name"]

        # Check for edit_file in plan mode
        if function_name == "edit_file" and self.chat_manager.interaction_mode == "plan":
            return False, "exit_code=1\nedit_file is disabled in plan mode. Focus on theoretical outlines and provide a summary of changes at the end."

        # Parse arguments
        try:
            args_str = tool_call["function"]["arguments"]
            if args_str is None:
                return False, "Error: Tool arguments are missing."
            arguments = json.loads(args_str)
        except (json.JSONDecodeError, TypeError):
            return False, "Error: Invalid JSON arguments."

        # Create SubAgentPanel for sub_agent tool calls
        panel_to_use = self.panel_updater
        if function_name == "sub_agent":
            query = arguments.get("query", "")
            panel_to_use = SubAgentPanel(query, self.console)

        # Execute via tool registry
        from tools.base import ToolRegistry, build_context

        tool = ToolRegistry.get(function_name)
        if tool:
            try:
                context = build_context(
                    repo_root=self.repo_root,
                    console=self.console,
                    gitignore_spec=self.gitignore_spec,
                    debug_mode=self.debug_mode,
                    interaction_mode=self.chat_manager.interaction_mode,
                    chat_manager=self.chat_manager,
                    rg_exe_path=self.rg_exe_path,
                    panel_updater=panel_to_use,
                    vault_root=_vault_root_str()
                )
                # Determine terminal policy for thinking indicator management
                from tools.helpers.base import get_terminal_policy, TERMINAL_YIELD
                policy = get_terminal_policy(function_name)

                # Check if tool requires approval
                if tool.requires_approval:
                    # For edit_file: generate preview and request approval
                    if function_name == "edit_file":
                        from rich.text import Text

                        result = tool.execute(arguments, context)

                        # Display preview
                        console = self._get_console()
                        if console:
                            if isinstance(result, Text):
                                # Rich Text object (new format with styling)
                                approved_result, should_exit = self._handle_edit_approval(
                                    result, arguments.get('path', ''), arguments,
                                    console, thinking_indicator)
                                if should_exit:
                                    return True, approved_result
                                result = approved_result
                            elif result.startswith("exit_code=0"):
                                # Legacy string format - parse and display
                                lines = result.split('\n')
                                preview_lines = [line for line in lines if not line.startswith("exit_code=")]
                                preview = '\n'.join(preview_lines).strip()

                                approved_result, should_exit = self._handle_edit_approval(
                                    preview, arguments.get('path', ''), arguments,
                                    console, thinking_indicator)
                                if should_exit:
                                    return True, approved_result
                                result = approved_result
                            else:
                                # Error occurred during preview - don't show to user, but still return to agent
                                pass
                        return False, str(result)
                    elif function_name == "execute_command":
                        # Get console for approval prompt
                        console = self._get_console()

                        # Check if command should be silently blocked (redirect to native tool)
                        # This check happens BEFORE approval to avoid prompting for blocked commands
                        from utils.validation import is_auto_approved_command, check_for_silent_blocked_command
                        command = arguments.get('command', '')
                        is_blocked, reprompt_msg = check_for_silent_blocked_command(command)
                        if is_blocked:
                            # Return reprompt message to guide the AI to use the native tool
                            # This is not shown to the user - the AI sees it and can retry
                            if self.debug_mode:
                                console.print(f"[dim]Silently blocked command: {command.split()[0]}[/dim]")
                            result = f"exit_code=1\n{reprompt_msg}"
                            return False, result

                        # Check if command should be auto-approved
                        auto_approve = is_auto_approved_command(command)

                        # Request user approval (unless auto-approved)
                        if not auto_approve:
                            # Stop thinking indicator while waiting for user input
                            if thinking_indicator:
                                thinking_indicator.stop()

                            action, guidance = confirm_tool(
                                f"execute_command: {command[:80]}{'...' if len(command) > 80 else ''}",
                                console,
                                reason=arguments.get('reason', 'Execute shell command'),
                                requires_approval=True,
                                approve_mode=self.chat_manager.approve_mode
                            )

                            if action == "accept":
                                result = tool.execute(arguments, context)
                            elif action == "advise":
                                result = f"Command not executed. User advice: {guidance}"
                            elif action == "cancel":
                                result = "Command canceled by user. Do not retry this operation."
                                # Break the agentic loop entirely
                                return True, result

                            # Restart thinking indicator after user input
                            if thinking_indicator:
                                thinking_indicator.start()
                        else:
                            # Auto-approved command - execute without prompting
                            result = tool.execute(arguments, context)
                    else:
                        # Other tools with requires_approval can be handled here in the future
                        result = tool.execute(arguments, context)
                else:
                    # No approval required - execute normally
                    # Handle thinking indicator based on tool's terminal policy
                    if policy == TERMINAL_YIELD and thinking_indicator:
                        thinking_indicator.pause()
                        # Force print to clear the status line
                        temp_console = self._get_console()
                        temp_console.print()
                        temp_console.file.flush()
                    
                    result = tool.execute(arguments, context)
                    
                    # Resume thinking indicator for yield policy
                    if policy == TERMINAL_YIELD and thinking_indicator:
                        thinking_indicator.resume()

                # Display result for registry tools
                # Skip display for tools that take over the terminal (they handle their own display)
                if policy != TERMINAL_YIELD:
                    console = self._get_console()
                    if console:
                        # Build label with arguments for better display
                        label = _build_tool_label(function_name, arguments)

                        # Print label first (like parallel mode)
                        label_text = f"[grey]{label}[/grey]" if not function_name.startswith("web search") else f"[bold cyan]{label}[/bold cyan]"
                        if not self.panel_updater:
                            console.print(label_text, highlight=False)
                            console.file.flush()

                        # Then display feedback
                        _display_tool_feedback(label, result, console, indent=self.is_sub_agent, panel_updater=self.panel_updater)

                return False, str(result)
            except Exception as e:
                return False, f"Error executing tool '{function_name}': {str(e)}"

        return False, f"Error: Unknown tool '{function_name}'."

def agentic_answer(chat_manager, user_input, console, repo_root, rg_exe_path, debug_mode, thinking_indicator=None):
    """Main agent loop using OpenAI-style function calling.

    This is a convenience wrapper that creates an AgenticOrchestrator
    and runs it with the provided parameters.

    Args:
        chat_manager: ChatManager instance
        user_input: User's input message
        console: Rich console for output
        repo_root: Path to repository root
        rg_exe_path: Path to rg.exe
        debug_mode: Whether to show debug output
        thinking_indicator: Optional ThinkingIndicator instance
    """
    orchestrator = AgenticOrchestrator(
        chat_manager=chat_manager,
        repo_root=repo_root,
        rg_exe_path=rg_exe_path,
        console=console,
        debug_mode=debug_mode,
    )
    orchestrator.run(user_input, thinking_indicator)


