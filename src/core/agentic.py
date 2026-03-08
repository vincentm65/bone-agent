"""Agent tool-calling loop."""

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text
from rich.live import Live
from prompt_toolkit.formatted_text import HTML
from utils.markdown import left_align_headings
from llm.config import TOOLS_REQUIRE_CONFIRMATION, WEB_SEARCH_REQUIRE_CONFIRMATION
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
from utils.web_search import run_web_search
from ui.prompt_utils import create_confirmation_prompt_session
from exceptions import (
    LLMError,
    LLMConnectionError,
    LLMResponseError,
    CommandExecutionError,
    FileEditError,
)


def _get_exit_code(tool_result):
    if not isinstance(tool_result, str):
        return None
    first_line = tool_result.splitlines()[0] if tool_result else ""
    if first_line.startswith("exit_code="):
        try:
            value = first_line.split("=", 1)[1].strip()
            value = value.split()[0] if value else value
            return int(value)
        except ValueError:
            return None
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


MAX_TASKS = 50
MAX_TASK_LEN = 200

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
MAX_TASK_TITLE_LEN = 80


def _format_task_list(task_list, title=None):
    if not task_list:
        return "exit_code=1\nerror: No task list exists. Use create_task_list first.\n\n"

    safe_title = (title or "").strip() if isinstance(title, str) else ""
    safe_title = safe_title[:MAX_TASK_TITLE_LEN] if safe_title else "untitled"

    done_count = 0
    lines = [f"Task list: {safe_title} (done={done_count} total={len(task_list)})"]

    for i, task in enumerate(task_list):
        is_done = bool(task.get("completed"))
        if is_done:
            done_count += 1
        checkbox = "[x]" if is_done else "[ ]"
        desc = str(task.get("description", ""))
        if len(desc) > MAX_TASK_LEN:
            desc = desc[:MAX_TASK_LEN - 3] + "..."
        lines.append(f"{i}: {checkbox} {desc}")

    # Update header with final done_count
    lines[0] = f"Task list: {safe_title} (done={done_count} total={len(task_list)})"
    return "\n".join(lines) + "\n\n"


def _strip_leading_task_list_echo(content, task_list, title=None):
    """Remove a leading echoed task list from assistant content.

    Some models copy the task list tool output into the final response, which
    causes duplicate task list rendering in the CLI.
    """
    if not content or not isinstance(content, str) or not task_list:
        return content

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
        exit_code_match = re.search(r'exit_code=(\d+)', lines[0])
        exit_code = int(exit_code_match.group(1)) if exit_code_match else None

        # Get output (all lines after the exit_code line)
        output_lines = lines[1:] if exit_code_match else lines
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
        else:
            tool_name = command.split()[0]
        
        # Pass to panel updater which will handle formatting
        panel_updater.add_tool_call(tool_name, tool_result, command)

    # For task list tools: show the list (bounded by MAX_TASKS / MAX_TASK_LEN)
    if command.startswith(("create_task_list", "complete_task", "show_task_list")):
        exit_code = _get_exit_code(tool_result)
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
        first_line = tool_result.split('\n')[0]
        match = re.search(r'lines_read=(\d+)', first_line)
        start_match = re.search(r'start_line=(\d+)', first_line)
        if match:
            count = int(match.group(1))
            # Only add prefix for console, not for panel_updater
            prefix = "╰─ " if not panel_updater else ""

            # Build message with line range if start_line is present
            if start_match:
                start_line = int(start_match.group(1))
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
        lines = tool_result.split('\n')
        prefix = "╰─ " if not panel_updater else ""
        message = None

        # Check for "No matches found" message (0 results)
        if any("No matches found" in line for line in lines):
            message = f"{prefix}[dim]No matches found[/dim]"
        # Check for matches=N or files=N pattern
        elif len(lines) > 1:
            match = re.search(r'(matches|files)=(\d+)', lines[1])
            if match:
                count = int(match.group(2))
                label = match.group(1)
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
            first_line = tool_result.split('\n')[0]
            match = re.search(r'lines_read=(\d+)', first_line)
            start_match = re.search(r'start_line=(\d+)', first_line)
            
            if match:
                count = int(match.group(1))
                if start_match:
                    start_line = int(start_match.group(1))
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
                match = re.search(r'(matches|files)=(\d+)', lines[1])
                if match:
                    count = int(match.group(2))
                    label = match.group(1)
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
        
        elif tool_name in ("create_task_list", "complete_task", "show_task_list"):
            # Handle task list tools - show the task list content
            exit_code = _get_exit_code(tool_result)
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


    def _get_console(self):
        """Get the console for output, respecting parallel execution context.

        Returns:
            Console object or None if suppressed during parallel execution
        """
        # Check if we're in a parallel context with suppressed console
        return self._parallel_context.get('console', self.console)

    def _safe_print(self, message, indent=False):
        """Print to console if not suppressed.

        Args:
            message: Message to print
            indent: If True, prefix with '│ ' when in sub-agent mode
        """
        # During parallel execution, suppress ALL output (we manage display ourselves)
        console = self._get_console()
        if console is None:
            return
            
        if self.panel_updater:
            # For sub-agent panel, non-tool messages (like warnings) are appended directly
            self.panel_updater.append(message)
        else:
            if indent and self.is_sub_agent:
                console.print(f"│ {message}", highlight=False)
            else:
                console.print(message, highlight=False)

    def run(self, user_input, thinking_indicator=None, allowed_tools=None):
        """Main orchestration loop.

        Args:
            user_input: User's input message
            thinking_indicator: Optional ThinkingIndicator instance
            allowed_tools: Optional list of allowed tool names (for research)
        """
        # Append user message
        self.chat_manager.messages.append({"role": "user", "content": user_input})
        user_msg_idx = len(self.chat_manager.messages) - 1

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
                if self._handle_final_response(response):
                    return
            else:
                should_exit = self._handle_tool_calls(response, thinking_indicator, allowed_tools)
                if should_exit:
                    return

    def _get_llm_response(self, allowed_tools=None):
        """Get next LLM response with tool definitions.

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

        try:
            response = self.chat_manager.client.chat_completion(
                self.chat_manager.messages, stream=False, tools=tools
            )
        except LLMError as e:
            self.console.print(f"[red]LLM Error: {e}[/red]")
            return None

        # Extract and track usage data
        if isinstance(response, dict) and 'usage' in response:
            self.chat_manager.token_tracker.add_usage(response['usage'])

        try:
            message = response["choices"][0]["message"]
        except (KeyError, IndexError):
            self.console.print("[red]Error: invalid response from model[/red]")
            return None

        return message

    def _handle_final_response(self, response):
        """Handle non-tool-call response (final answer).

        Args:
            response: Message dict from LLM

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

        if use_parallel:
            return self._execute_tools_parallel(response, thinking_indicator)
        else:
            return self._execute_tools_sequential(tool_calls, thinking_indicator)

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
                end_loop = True

            # Append tool result if not skipped (guidance mode)
            if tool_result is not None and tool_result is not False:
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": str(tool_result)
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
                    except:
                        label_text = f"[grey]{function_name}[/grey]"
                        if not self.panel_updater:
                            self.console.print(label_text, highlight=False)
                            self.console.file.flush()

                    # Display feedback immediately after label (no buffering)
                    try:
                        if function_name == "edit_file" and result.requires_approval:
                            # Handle approval workflow for edit_file in parallel mode
                            # Display the preview diff
                            from rich.text import Text

                            # Check if result is a Text object (new format with styling)
                            if isinstance(result.result, Text):
                                # Extract the first line to check exit code
                                plain = str(result.result).split('\n')[0] if result.result else ""
                                if plain.startswith("exit_code=0"):
                                    # Display the Rich Text object directly
                                    self.console.print(result.result)
                                    self.console.print()

                                    # Request user approval
                                    # Stop thinking indicator while waiting for user input
                                    thinking_indicator = context.get('thinking_indicator')
                                    if thinking_indicator:
                                        thinking_indicator.stop()

                                    # Create confirmation prompt session with toolbar
                                    prompt_session = create_confirmation_prompt_session(
                                        self.chat_manager,
                                        lambda: HTML("<b>Approve edit? (y/n/guidance): </b>")
                                    )

                                    action, guidance = confirm_tool(
                                        f"edit_file: {args_dict.get('path', '')}",
                                        self.console,
                                        reason=args_dict.get('reason', 'Apply file edit with above changes'),
                                        requires_approval=True,
                                        prompt_session=prompt_session,
                                        approve_mode=self.chat_manager.approve_mode
                                    )

                                    if action == "execute":
                                        # User approved - execute the edit
                                        from tools.edit import _execute_edit_file
                                        final_result = _execute_edit_file(
                                            path=args_dict.get('path'),
                                            search=args_dict.get('search'),
                                            replace=args_dict.get('replace'),
                                            repo_root=self.repo_root,
                                            console=self.console,
                                            gitignore_spec=self.gitignore_spec,
                                            context_lines=args_dict.get('context_lines', 3)
                                        )
                                        # Replace result with final result
                                        result.result = final_result
                                    elif action == "reject":
                                        result.result = "exit_code=1\nEdit rejected by user."
                                    elif action == "guide":
                                        result.result = f"exit_code=1\nEdit not applied. User guidance: {guidance}"

                                    # Restart thinking indicator after user input
                                    if thinking_indicator:
                                        thinking_indicator.start()
                            elif result.result.startswith("exit_code=0"):
                                # Legacy string format - parse and display
                                lines = result.result.split('\n')
                                preview_lines = [line for line in lines if not line.startswith("exit_code=")]
                                preview = '\n'.join(preview_lines).strip()

                                # Display preview to user
                                self.console.print(preview)
                                self.console.print()

                                # Request user approval
                                # Stop thinking indicator while waiting for user input
                                thinking_indicator = context.get('thinking_indicator')
                                if thinking_indicator:
                                    thinking_indicator.stop()

                                # Create confirmation prompt session with toolbar
                                prompt_session = create_confirmation_prompt_session(
                                    self.chat_manager,
                                    lambda: HTML("<b>Approve edit? (y/n/guidance): </b>")
                                )

                                action, guidance = confirm_tool(
                                    f"edit_file: {args_dict.get('path', '')}",
                                    self.console,
                                    reason=args_dict.get('reason', 'Apply file edit with above changes'),
                                    requires_approval=True,
                                    prompt_session=prompt_session
                                )

                                if action == "execute":
                                    # User approved - execute the edit
                                    from tools.edit import _execute_edit_file
                                    final_result = _execute_edit_file(
                                        path=args_dict.get('path'),
                                        search=args_dict.get('search'),
                                        replace=args_dict.get('replace'),
                                        repo_root=self.repo_root,
                                        console=self.console,
                                        gitignore_spec=self.gitignore_spec,
                                        context_lines=args_dict.get('context_lines', 3)
                                    )
                                    # Replace result with final result
                                    result.result = final_result
                                elif action == "reject":
                                    result.result = "exit_code=1\nEdit rejected by user."
                                elif action == "guide":
                                    result.result = f"exit_code=1\nEdit not applied. User guidance: {guidance}"

                                # Restart thinking indicator after user input
                                if thinking_indicator:
                                    thinking_indicator.start()
                            else:
                                # Error occurred during preview - just show it
                                self.console.print(result.result)
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
                    except:
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

                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": result.tool_id,
                        "content": str(result.result)
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
                    panel_updater=panel_to_use
                )

                # Check if tool requires approval
                if tool.requires_approval:
                    # For edit_file: generate preview and request approval
                    if function_name == "edit_file":
                        from rich.text import Text

                        result = tool.execute(arguments, context)

                        # Display preview
                        console = self._get_console()
                        if console:
                            # Check if result is a Text object (new format with styling)
                            if isinstance(result, Text):
                                # Extract the first line to check exit code
                                plain = str(result).split('\n')[0] if result else ""
                                if plain.startswith("exit_code=0"):
                                    # Display the Rich Text object directly
                                    console.print(result)
                                    console.print()

                                    # Request user approval
                                    # Stop thinking indicator while waiting for user input
                                    if thinking_indicator:
                                        thinking_indicator.stop()

                                    # Create confirmation prompt session with toolbar
                                    prompt_session = create_confirmation_prompt_session(
                                        self.chat_manager,
                                        lambda: HTML("<b>Approve edit? (y/n/guidance): </b>")
                                    )

                                    action, guidance = confirm_tool(
                                        f"edit_file: {arguments.get('path', '')}",
                                        console,
                                        reason=arguments.get('reason', 'Apply file edit with above changes'),
                                        requires_approval=True,
                                        prompt_session=prompt_session,
                                        approve_mode=self.chat_manager.approve_mode
                                    )

                                    if action == "execute":
                                        # User approved - execute the edit
                                        from tools.edit import _execute_edit_file
                                        result = _execute_edit_file(
                                            path=arguments.get('path'),
                                            search=arguments.get('search'),
                                            replace=arguments.get('replace'),
                                            repo_root=self.repo_root,
                                            console=console,
                                            gitignore_spec=self.gitignore_spec,
                                            context_lines=arguments.get('context_lines', 3)
                                        )
                                    elif action == "reject":
                                        result = "exit_code=1\nEdit rejected by user."
                                    elif action == "guide":
                                        result = f"exit_code=1\nEdit not applied. User guidance: {guidance}"

                                    # Restart thinking indicator after user input
                                    if thinking_indicator:
                                        thinking_indicator.start()
                            # Show the preview diff
                            elif result.startswith("exit_code=0"):
                                # Extract and display the diff preview
                                lines = result.split('\n')
                                preview_lines = [line for line in lines if not line.startswith("exit_code=")]
                                preview = '\n'.join(preview_lines).strip()

                                # Display preview to user
                                console.print(preview)
                                console.print()

                                # Request user approval
                                # Stop thinking indicator while waiting for user input
                                if thinking_indicator:
                                    thinking_indicator.stop()

                                # Create confirmation prompt session with toolbar
                                prompt_session = create_confirmation_prompt_session(
                                    self.chat_manager,
                                    lambda: HTML("<b>Approve edit? (y/n/guidance): </b>")
                                )

                                action, guidance = confirm_tool(
                                    f"edit_file: {arguments.get('path', '')}",
                                    console,
                                    reason=arguments.get('reason', 'Apply file edit with above changes'),
                                    requires_approval=True,
                                    prompt_session=prompt_session,
                                    approve_mode=self.chat_manager.approve_mode
                                )

                                if action == "execute":
                                    # User approved - execute the edit
                                    from tools.edit import _execute_edit_file
                                    result = _execute_edit_file(
                                        path=arguments.get('path'),
                                        search=arguments.get('search'),
                                        replace=arguments.get('replace'),
                                        repo_root=self.repo_root,
                                        console=console,
                                        gitignore_spec=self.gitignore_spec,
                                        context_lines=arguments.get('context_lines', 3)
                                    )
                                elif action == "reject":
                                    result = "exit_code=1\nEdit rejected by user."
                                elif action == "guide":
                                    result = f"exit_code=1\nEdit not applied. User guidance: {guidance}"

                                # Restart thinking indicator after user input
                                if thinking_indicator:
                                    thinking_indicator.start()
                            else:
                                # Error occurred during preview - just show it
                                if console:
                                    console.print(result)
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

                            # Create confirmation prompt session with toolbar
                            prompt_session = create_confirmation_prompt_session(
                                self.chat_manager,
                                lambda: HTML("<b>Approve command? (y/n/guidance): </b>")
                            )

                            action, guidance = confirm_tool(
                                f"execute_command: {command[:80]}{'...' if len(command) > 80 else ''}",
                                console,
                                reason=arguments.get('reason', 'Execute shell command'),
                                requires_approval=True,
                                prompt_session=prompt_session
                            )

                            if action == "execute":
                                result = tool.execute(arguments, context)
                            elif action == "reject":
                                result = "Command rejected by user."
                            elif action == "guide":
                                result = f"Command not executed. User guidance: {guidance}"

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
                    # Pause thinking indicator for sub_agent (it has its own)
                    if function_name == "sub_agent" and thinking_indicator:
                        thinking_indicator.pause()
                    
                    result = tool.execute(arguments, context)
                    
                    # Resume thinking indicator after sub_agent completes
                    if function_name == "sub_agent" and thinking_indicator:
                        thinking_indicator.resume()

                # Display result for registry tools
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


