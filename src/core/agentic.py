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
from utils.markdown import left_align_headings
from llm.config import TOOLS_REQUIRE_CONFIRMATION, WEB_SEARCH_REQUIRE_CONFIRMATION
from utils.settings import MAX_TOOL_CALLS, MonokaiDarkBGStyle
from utils.validation import check_for_duplicate, check_command
from utils.tools import (
    run_shell_command,
    confirm_tool,
    run_edit_file,
    preview_edit_file,
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


def _is_truncated_result(tool_result):
    if not isinstance(tool_result, str):
        return False
    first_line = tool_result.splitlines()[0] if tool_result else ""
    return "truncated=true" in first_line


def _coerce_bool(value, default=None):
    """Best-effort coercion of tool arguments to boolean.

    Returns None if value is None and default is None.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off"}:
            return False
    return default


MAX_TASKS = 50
MAX_TASK_LEN = 200
MAX_TASK_TITLE_LEN = 80


def _coerce_int(value):
    """Best-effort coercion of tool arguments to int.

    Returns (int_value, error_message). error_message is None on success.
    """
    if value is None:
        return None, "Missing required integer value."
    if isinstance(value, bool):
        return None, "Value must be an integer, not a boolean."
    if isinstance(value, int):
        return value, None
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return None, "Value must be a non-empty integer."
        try:
            return int(text), None
        except ValueError:
            return None, "Value must be an integer."
    return None, "Value must be an integer."


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
    import re

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
            if not panel_updater:
                console.print(rendered.strip(), markup=False)
        else:
            # Show single-line error if present
            first_two = "\n".join(tool_result.splitlines()[:2]).strip()
            if not panel_updater:
                console.print(first_two or tool_result.strip(), markup=False)
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
            
            if panel_updater:
                panel_updater.append(message)
            else:
                console.print(message)
        if not panel_updater:
            console.print()
        return

        # For rg: parse matches/files from second line
    if command.startswith("rg"):
        lines = tool_result.split('\n')
        if len(lines) > 1:
            match = re.search(r'(matches|files)=(\d+)', lines[1])
            if match:
                count = int(match.group(2))
                label = match.group(1)
                # Only add prefix for console, not for panel_updater
                prefix = "╰─ " if not panel_updater else ""
                if count == 0:
                    message = f"{prefix}[dim]No {label} found[/dim]"
                else:
                    message = f"{prefix}[dim]Found {count} {label}[/dim]"
                if panel_updater:
                    panel_updater.append(message)
                else:
                    console.print(message)
        if not panel_updater:
            console.print()
        return

    # For list_directory: parse and display directory tree
    if command.startswith("list_directory"):
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
            
            # Parse entries: kind, size, name
            entries = []
            for line in content_lines:
                parts = line.split()
                if len(parts) >= 3:
                    kind = parts[0]
                    if kind == "FILE":
                        # FILE  12345 bytes  path/to/file.py
                        size = parts[1]
                        name = ' '.join(parts[3:])  # everything after "bytes"
                        entries.append(("FILE", name, size))
                    elif kind == "DIR":
                        # DIR              path/to/dir/
                        name = ' '.join(parts[1:])
                        entries.append(("DIR", name))
            
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
            
            if panel_updater:
                panel_updater.append(output)
            else:
                console.print(output)
        if not panel_updater:
            console.print()
        return

    # For create_file: display preview of created file
    if command.startswith("create_file"):
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
            # Map common extensions to lexer names
            lexer_map = {
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
            lexer_name = lexer_map.get(file_ext.lower(), 'text')

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
            if panel_updater:
                panel_updater.append(message)
            else:
                console.print(message)

        if not panel_updater:
            console.print()
        return

    # For execute_command: display command output with line truncation
    if command.startswith("execute_command"):
        lines = tool_result.split('\n')
        if lines:
            # Extract exit code from first line
            exit_code_match = re.search(r'exit_code=(\d+)', lines[0])
            exit_code = int(exit_code_match.group(1)) if exit_code_match else None
            
            # Get output (all lines after the exit_code line)
            output_lines = lines[1:] if exit_code_match else lines
            output_lines = [line for line in output_lines if line.strip()]
            
            from utils.settings import MAX_COMMAND_OUTPUT_LINES
            
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
                if panel_updater:
                    panel_updater.append(display_text)
                else:
                    console.print(display_text, markup=False)

            # Show truncation message separately to preserve markup
            if truncation_message:
                if panel_updater:
                    panel_updater.append(truncation_message)
                else:
                    console.print(truncation_message)
            
            # Show exit code if non-zero
            if exit_code is not None and exit_code != 0:
                exit_text = f"[dim](exit code: {exit_code})[/dim]"
                if panel_updater:
                    panel_updater.append(exit_text)
                else:
                    console.print(exit_text)
        
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
            else:
                message = f"[grey]rg {pattern}[/grey]"
        
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
            if query:
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

        # Update panel with green complete title showing total tool calls
        self._live.update(self._render_panel(
            title=f"[green]✓ Sub-Agent Complete ({self.total_tool_calls})[/green]",
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

    def __init__(self, chat_manager, repo_root, rg_exe_path, console, debug_mode, suppress_result_display=False, is_sub_agent=False, panel_updater=None, pre_tool_planning_enabled=False, force_parallel_execution=False):
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
            pre_tool_planning_enabled: If True, enable pre-tool planning step
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
        self.pre_tool_planning_enabled = pre_tool_planning_enabled
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
            tools = [tool for tool in TOOLS if tool["function"]["name"] in allowed_tools]
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
        content = (response.get("content") or "").strip()
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

        # Append assistant message with tool calls (include content if present)
        assistant_msg = {"role": "assistant", "tool_calls": tool_calls}
        if content:
            assistant_msg["content"] = content
        self.chat_manager.messages.append(assistant_msg)
        # Log assistant tool call message
        self.chat_manager.log_message(assistant_msg)

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
                    "content": tool_result
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
        from utils.tools.parallel_executor import ParallelToolExecutor, ToolCall

        # Suppress console output in handlers during parallel execution
        # We'll display results ourselves in order below
        self._parallel_context['console'] = None

        try:
            # Build handler map
            handler_map = {
                "rg": self._handle_rg,
                "read_file": self._handle_read_file,
                "list_directory": self._handle_list_directory,
                "create_file": self._handle_create_file,
                "edit_file": self._handle_edit_file,
                "web_search": self._handle_web_search,
                "sub_agent": self._handle_sub_agent,
                "create_task_list": self._handle_create_task_list,
                "complete_task": self._handle_complete_task,
                "show_task_list": self._handle_show_task_list,
            }

            # Prepare context
            context = {
                'thinking_indicator': thinking_indicator,
                'repo_root': self.repo_root,
                'chat_manager': self.chat_manager,
                'rg_exe_path': self.rg_exe_path,
                'debug_mode': self.debug_mode,
                'gitignore_spec': self.gitignore_spec,
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
                handler_map,
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
                            a.get('path', ''),
                            a.get('start_line'),
                            a.get('max_lines'),
                            with_colon=True
                        ),
                        "list_directory": lambda a: f"list_directory: {a.get('path', '')}",
                        "create_file": lambda a: f"create_file: {a.get('path', '')}",
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
                        if function_name == "edit_file":
                            pass  # Edit results displayed by preview
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
                        "content": result.result
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
            arguments = json.loads(tool_call["function"]["arguments"])
        except json.JSONDecodeError:
            return False, "Error: Invalid JSON arguments."

        # Route to appropriate handler
        handler = self._get_tool_handler(function_name)
        if handler:
            return handler(tool_id, arguments, thinking_indicator)
        else:
            return False, f"Error: Unknown tool '{function_name}'."

    def _get_tool_handler(self, function_name):
        """Return handler function for given tool name.

        Args:
            function_name: Name of the tool function

        Returns:
            Handler method or None
        """
        handlers = {
            "rg": self._handle_rg,
            "execute_command": self._handle_execute_command,
            "read_file": self._handle_read_file,
            "list_directory": self._handle_list_directory,
            "create_file": self._handle_create_file,
            "edit_file": self._handle_edit_file,
            "web_search": self._handle_web_search,
            "sub_agent": self._handle_sub_agent,
            "create_task_list": self._handle_create_task_list,
            "complete_task": self._handle_complete_task,
            "show_task_list": self._handle_show_task_list,
        }
        return handlers.get(function_name)

    def _handle_rg(self, tool_id, arguments, thinking_indicator):
        """Handle rg (ripgrep) tool call.

        Args:
            tool_id: Tool call ID
            arguments: Tool arguments dict
            thinking_indicator: Optional ThinkingIndicator instance

        Returns:
            Tuple of (should_exit, tool_result)
        """
        pattern = arguments.get("pattern", "")
        if not isinstance(pattern, str) or not pattern.strip():
            return False, "exit_code=1\nrg requires a non-empty 'pattern' argument."

        # Build rg command from arguments
        cmd_parts = ["rg"]

        # Add --line-number for all searches
        cmd_parts.append("--line-number")

        # Add max-filesize if specified
        if arguments.get("max_filesize"):
            cmd_parts.append(f"--max-filesize={arguments['max_filesize']}")

        # Add context if specified
        context = arguments.get("context")
        if context:
            if context.startswith("before:"):
                try:
                    n = int(context.split(":", 1)[1])
                    cmd_parts.append(f"--before-context={n}")
                except (ValueError, IndexError):
                    pass
            elif context.startswith("after:"):
                try:
                    n = int(context.split(":", 1)[1])
                    cmd_parts.append(f"--after-context={n}")
                except (ValueError, IndexError):
                    pass
            elif context.startswith("both:"):
                try:
                    n = int(context.split(":", 1)[1])
                    cmd_parts.append(f"--context={n}")
                except (ValueError, IndexError):
                    pass

        # Add file type filter if specified
        file_type = arguments.get("type")
        if file_type:
            cmd_parts.append(f"--type={file_type}")

        # Add files-with-matches flag if specified
        files_with_matches = _coerce_bool(arguments.get("files_with_matches"), default=False)
        if files_with_matches:
            cmd_parts.append("--files-with-matches")

        # Add pattern - quote if it contains spaces to prevent splitting by shlex
        import shlex
        if " " in pattern:
            cmd_parts.append(shlex.quote(pattern))
        else:
            cmd_parts.append(pattern)

        # Add path (default to current directory)
        path = arguments.get("path") or "."
        cmd_parts.append(path)

        # Build command string
        command = " ".join(cmd_parts)

        # Check for duplicates
        is_duplicate, redirect_msg = check_for_duplicate(self.chat_manager, command)
        if is_duplicate:
            if self.debug_mode:
                self._safe_print("[yellow]Duplicate command[/yellow]")
            return False, redirect_msg

        # Execute the rg command (skip full output, show summary only)
        should_exit, tool_result = self._execute_approved_command(command, indent=self.is_sub_agent, skip_full_output=True)

        return should_exit, tool_result

    def _handle_execute_command(self, tool_id, arguments, thinking_indicator):
        """Handle execute_command tool call.

        Args:
            tool_id: Tool call ID
            arguments: Tool arguments dict
            thinking_indicator: Optional ThinkingIndicator instance

        Returns:
            Tuple of (should_exit, tool_result)
        """
        if "command" not in arguments:
            return False, "Error: Missing 'command' argument."

        command = arguments["command"]
        if not isinstance(command, str) or not command.strip():
            return False, "Error: 'command' argument must be a non-empty string."

        # Check for duplicates
        is_duplicate, redirect_msg = check_for_duplicate(self.chat_manager, command)
        if is_duplicate:
            if self.debug_mode:
                self.console.print("[yellow]Duplicate command[/yellow]")
            return False, redirect_msg

        # Validate command (check_command allows git, rg, file operations)
        is_safe, reason = check_command(
            command, self.chat_manager.approve_mode
        )

        if not is_safe:
            return False, reason

        # All shell and file operation commands require approval
        # Strip "powershell " prefix for command name detection
        cmd_for_check = command.strip()
        if cmd_for_check.lower().startswith("powershell "):
            cmd_for_check = cmd_for_check[11:].strip()

        # Tokenize to get command name
        import shlex
        use_posix = os.name != "nt"
        tokens = shlex.split(cmd_for_check, posix=use_posix) if cmd_for_check else []
        cmd_name = tokens[0].lower() if tokens else ""

        # Path traversal warnings (show but don't block)
        if self.debug_mode:
            # Detect absolute paths outside common safe directories
            abs_paths = []
            for token in tokens:
                # Unix absolute paths
                if token.startswith('/') and not token.startswith(('/home', '/tmp', '/var/log')):
                    abs_paths.append(token)
                # Windows absolute paths
                elif len(token) >= 3 and token[1:3] == ':\\' and not token[:3].lower().startswith(('c:\\users', 'd:\\users', 'e:\\users')):
                    abs_paths.append(token)
            
            if abs_paths:
                self.console.print(f"[yellow]⚠ Path traversal detected: {abs_paths[0]}[/yellow]")
            
            # Warn about && chaining
            if "&&" in command:
                self.console.print("[dim]→ Using && for conditional chaining[/dim]")

        # Check if command requires approval (whitelist: only safe commands don't require approval)
        from llm.config import ALLOWED_COMMANDS

        requires_approval = (
            cmd_name not in ALLOWED_COMMANDS
        )

        # Special case: git status, diff, log, show, blame are auto-approved
        # branch is allowed for listing, but not deleting
        if cmd_name == "git" and requires_approval:
            git_subcommand = tokens[1].lower() if len(tokens) > 1 else ""
            if git_subcommand in ("status", "diff", "log", "show", "blame"):
                requires_approval = False
            elif git_subcommand == "branch" and not any(arg in tokens for arg in ("-d", "-D", "--delete")):
                requires_approval = False

        if requires_approval:
            # Require approval for all commands not in the allowed whitelist
            return self._handle_command_confirmation(command, None, thinking_indicator, requires_approval)
        else:
            # Allowed commands execute without approval
            return self._execute_approved_command(command, indent=self.is_sub_agent, skip_full_output=False)

    def _execute_approved_command(self, command, indent=False, skip_full_output=False):
        """Execute an approved command.

        Args:
            command: Command string to execute
            indent: If True, prefix output with '│ ' (for sub-agent mode)
            skip_full_output: If True, skip displaying full command output (for rg in single-tool mode)

        Returns:
            Tuple of (should_exit, tool_result)
        """
        # Get console respecting parallel context
        console = self._get_console()

        if self.panel_updater:
            # Extract tool name from command
            tool_name = "execute_command"
            if command.strip().startswith("rg"):
                tool_name = "rg"
            # Note: _display_tool_feedback will add formatted message
        elif console:
            # Print to console in normal mode (only if not suppressed)
            console.print(f"[grey]{command}[/grey]", highlight=False)
        try:
            tool_result = run_shell_command(
                command, self.repo_root, self.rg_exe_path,
                self.console, self.debug_mode
            )
            # Only display feedback if console is available (not in parallel mode)
            if console:
                if skip_full_output:
                    # For rg in single-tool mode: display summary only, not full output
                    _display_tool_feedback(command, tool_result, console, indent=indent, panel_updater=self.panel_updater)
                else:
                    # For execute_command tool: display full output with truncation
                    label = f"execute_command {command}"
                    _display_tool_feedback(label, tool_result, console, indent=indent, panel_updater=self.panel_updater)
        except CommandExecutionError as e:
            tool_result = f"exit_code=1\nError: {e}"
            if self.panel_updater:
                self.panel_updater.append(f"[red]Command failed: {e}[/red]")
            elif console:
                console.print(f"Command failed: {e}", style="red")

        # Only add blank line for non-rg commands (rg commands are compact)
        is_rg_command = command.strip().startswith("rg")
        if not is_rg_command:
            if self.panel_updater:
                self.panel_updater.append("")  # Blank line in panel
            elif console:
                console.print()
        return False, tool_result

    def _handle_command_confirmation(self, command, reason, thinking_indicator, requires_approval=True):
        """Handle command confirmation workflow.

        Args:
            command: Command string
            reason: Reason for confirmation required
            thinking_indicator: Optional ThinkingIndicator instance
            requires_approval: Whether this command specifically requires approval

        Returns:
            Tuple of (should_exit, tool_result)
        """
        if thinking_indicator:
            thinking_indicator.pause()
        confirmation_result, user_guidance = confirm_tool(command, self.console, reason, requires_approval=requires_approval, prompt_session=None)
        if thinking_indicator:
            thinking_indicator.resume()

        if confirmation_result == "execute":
            return self._execute_approved_command(command, skip_full_output=False)
        elif confirmation_result == "reject":
            return True, f"Tool request denied by user.\nCommand: {command}"
        elif confirmation_result == "guide":
            # Guidance mode - return tool result with user guidance
            # OpenAI API requires a tool response for every tool_call_id
            return False, f"User provided guidance: {user_guidance}"

    def _handle_read_file(self, tool_id, arguments, thinking_indicator):
        """Handle read_file tool call."""
        path = arguments.get("path", "")
        if not isinstance(path, str) or not path.strip():
            return False, "exit_code=1\nread_file requires a non-empty 'path' argument."

        # Validate path doesn't contain JSON-like syntax or invalid characters
        # This catches cases where malformed arguments are passed as file paths
        invalid_chars = '[]{}"\n\r\t'
        if any(char in path for char in invalid_chars):
            return False, f"exit_code=1\nread_file 'path' contains invalid characters. Got: {path}"

        max_lines = arguments.get("max_lines")
        if max_lines is not None:
            try:
                max_lines = int(max_lines)
            except (ValueError, TypeError):
                return False, "exit_code=1\nread_file 'max_lines' must be an integer."

        start_line = arguments.get("start_line")
        if start_line is not None:
            try:
                start_line = int(start_line)
            except (ValueError, TypeError):
                return False, "exit_code=1\nread_file 'start_line' must be an integer (1-based)."
        else:
            start_line = 1

        requested_mode = "partial" if max_lines is not None else "full"

        label = _build_read_file_label(path, start_line, max_lines)
        self._safe_print(f"[grey]{label}[/grey]", indent=self.is_sub_agent)

        tool_result = read_file(
            path,
            self.repo_root,
            max_lines=max_lines,
            start_line=start_line,
            gitignore_spec=self.gitignore_spec,
        )
        console = self._get_console()
        if console:
            _display_tool_feedback(label, tool_result, console, indent=self.is_sub_agent, panel_updater=self.panel_updater)

        if self.debug_mode:
            console = self._get_console()
            if console:
                console.print()
                console.print(f"[dim]→ AI receives:\n\n{tool_result}\n\n[/dim]")

        exit_code = _get_exit_code(tool_result)
        return False, tool_result

    def _handle_list_directory(self, tool_id, arguments, thinking_indicator):
        """Handle list_directory tool call."""
        path = arguments.get("path") or "."
        if not isinstance(path, str):
            return False, "exit_code=1\nlist_directory 'path' must be a string."

        # Validate path doesn't contain JSON-like syntax or invalid characters
        invalid_chars = '[]{}"\n\r\t'
        if any(char in path for char in invalid_chars):
            return False, f"exit_code=1\nlist_directory 'path' contains invalid characters. Got: {path}"

        recursive = _coerce_bool(arguments.get("recursive"), default=False)
        show_files = _coerce_bool(arguments.get("show_files"), default=True)
        show_dirs = _coerce_bool(arguments.get("show_dirs"), default=True)
        pattern = arguments.get("pattern")

        label = f"list_directory {path}"
        if recursive:
            label = f"{label} -recursive"
        self._safe_print(f"[grey]{label}[/grey]", indent=self.is_sub_agent)

        tool_result = list_directory(
            path,
            self.repo_root,
            recursive=bool(recursive),
            show_files=bool(show_files),
            show_dirs=bool(show_dirs),
            pattern=pattern,
            gitignore_spec=self.gitignore_spec,
        )
        console = self._get_console()
        if console:
            _display_tool_feedback(label, tool_result, console, indent=self.is_sub_agent, panel_updater=self.panel_updater)

        if self.debug_mode:
            console = self._get_console()
            if console:
                console.print()
                console.print(f"[dim]→ AI receives:\n\n{tool_result}\n\n[/dim]")

        return False, tool_result

    def _handle_create_file(self, tool_id, arguments, thinking_indicator):
        """Handle create_file tool call.

        Args:
            tool_id: Tool call ID
            arguments: Tool arguments dict
            thinking_indicator: Optional ThinkingIndicator instance

        Returns:
            Tuple of (should_exit, tool_result)
        """
        path = arguments.get("path", "")
        if not isinstance(path, str) or not path.strip():
            return False, "exit_code=1\ncreate_file requires a non-empty 'path' argument."

        # Validate path doesn't contain JSON-like syntax or invalid characters
        invalid_chars = '[]{}"\n\r\t'
        if any(char in path for char in invalid_chars):
            return False, f"exit_code=1\ncreate_file 'path' contains invalid characters. Got: {path}"

        content = arguments.get("content")

        label = f"create_file {path}"
        self._safe_print(f"[grey]{label}[/grey]")

        tool_result = create_file(
            path,
            self.repo_root,
            content=content,
            gitignore_spec=self.gitignore_spec
        )
        console = self._get_console()
        if console:
            _display_tool_feedback(label, tool_result, console)

        if self.debug_mode:
            console = self._get_console()
            if console:
                console.print()
                console.print(f"[dim]→ AI receives:\n\n{tool_result}\n\n[/dim]")

        return False, tool_result

    def _handle_create_task_list(self, tool_id, arguments, thinking_indicator):
        """Handle create_task_list tool call."""
        if self.chat_manager.interaction_mode == "plan":
            return False, "exit_code=1\nerror: Task lists are disabled in PLAN mode. Switch to EDIT mode.\n\n"

        tasks = arguments.get("tasks")
        if not isinstance(tasks, list):
            return False, "exit_code=1\nerror: 'tasks' must be an array of strings.\n\n"

        title = arguments.get("title")
        if title is not None and not isinstance(title, str):
            return False, "exit_code=1\nerror: 'title' must be a string.\n\n"
        title = title.strip() if isinstance(title, str) else None
        if title:
            title = title[:MAX_TASK_TITLE_LEN]

        normalized = []
        for i, task in enumerate(tasks):
            if not isinstance(task, str):
                return False, f"exit_code=1\nerror: Task at index {i} must be a string.\n\n"
            trimmed = task.strip()
            if not trimmed:
                return False, f"exit_code=1\nerror: Task at index {i} must be non-empty.\n\n"
            if len(trimmed) > MAX_TASK_LEN:
                return False, (
                    f"exit_code=1\nerror: Task at index {i} exceeds MAX_TASK_LEN={MAX_TASK_LEN}.\n\n"
                )
            normalized.append(trimmed)

        if len(normalized) == 0:
            return False, "exit_code=1\nerror: Provide at least one non-empty task.\n\n"
        if len(normalized) > MAX_TASKS:
            return False, f"exit_code=1\nerror: Too many tasks (max {MAX_TASKS}).\n\n"

        self.chat_manager.task_list = [
            {"description": t, "completed": False}
            for t in normalized
        ]
        self.chat_manager.task_list_title = title or None

        label = "create_task_list"
        tool_result = _format_task_list(self.chat_manager.task_list, self.chat_manager.task_list_title)
        console = self._get_console()
        if console:
            _display_tool_feedback(label, tool_result, console, panel_updater=self.panel_updater)
        return False, tool_result

    def _handle_complete_task(self, tool_id, arguments, thinking_indicator):
        """Handle complete_task tool call."""
        if self.chat_manager.interaction_mode == "plan":
            return False, "exit_code=1\nerror: Task lists are disabled in PLAN mode. Switch to EDIT mode.\n\n"

        task_id_raw = arguments.get("task_id")
        task_ids_raw = arguments.get("task_ids")

        # Normalize to list: prefer task_ids if both provided
        if task_ids_raw is not None:
            ids_raw = task_ids_raw
        elif task_id_raw is not None:
            ids_raw = [task_id_raw]
        else:
            return False, "exit_code=1\nerror: Either 'task_id' or 'task_ids' must be provided.\n\n"

        if not isinstance(ids_raw, list):
            return False, "exit_code=1\nerror: IDs must be an array of integers.\n\n"

        task_list = getattr(self.chat_manager, "task_list", None) or []
        if not task_list:
            return False, "exit_code=1\nerror: No task list exists. Use create_task_list first.\n\n"

        # Validate all IDs
        valid_ids = []
        for i, tid in enumerate(ids_raw):
            tid_int, error = _coerce_int(tid)
            if error:
                return False, f"exit_code=1\nerror: ID at index {i}: {error}\n\n"
            if tid_int < 0:
                return False, f"exit_code=1\nerror: ID at index {i} must be non-negative.\n\n"
            if tid_int >= len(task_list):
                return False, f"exit_code=1\nerror: ID {tid_int} (index {i}) is out of range (0-{len(task_list) - 1}).\n\n"
            valid_ids.append(tid_int)

        # Mark tasks as complete
        for tid in valid_ids:
            task_list[tid]["completed"] = True

        label = "complete_task"
        tool_result = _format_task_list(task_list, self.chat_manager.task_list_title)
        console = self._get_console()
        if console:
            _display_tool_feedback(label, tool_result, console, panel_updater=self.panel_updater)
        return False, tool_result

    def _handle_show_task_list(self, tool_id, arguments, thinking_indicator):
        """Handle show_task_list tool call."""
        if self.chat_manager.interaction_mode == "plan":
            return False, "exit_code=1\nerror: Task lists are disabled in PLAN mode. Switch to EDIT mode.\n\n"

        task_list = getattr(self.chat_manager, "task_list", None) or []
        title = getattr(self.chat_manager, "task_list_title", None)

        label = "show_task_list"
        tool_result = _format_task_list(task_list, title)
        console = self._get_console()
        if console:
            _display_tool_feedback(label, tool_result, console, panel_updater=self.panel_updater)
        return False, tool_result

    def _handle_edit_file(self, tool_id, arguments, thinking_indicator):
        """Handle edit_file tool call.

        Args:
            tool_id: Tool call ID
            arguments: Tool arguments dict
            thinking_indicator: Optional ThinkingIndicator instance

        Returns:
            Tuple of (should_exit, tool_result)
        """
        path = arguments.get("path", "")
        
        # Validate path doesn't contain JSON-like syntax or invalid characters
        invalid_chars = '[]{}"\n\r\t'
        if any(char in path for char in invalid_chars):
            return False, f"exit_code=1\nedit_file 'path' contains invalid characters. Got: {path}"
        


        # Preview edit
        try:
            preview_status, preview_diff = preview_edit_file(arguments, self.repo_root, self.gitignore_spec)
        except FileEditError as e:
            tool_result = f"exit_code=1\n{e}"
            self.console.print(f"Edit preview failed: {e}", style="red")
            return False, tool_result

        if preview_status != "exit_code=0":
            return False, preview_status

        # Display preview
        self.console.print(Text.from_ansi(preview_diff))
        self.console.print()

        search_text = arguments.get("search", "")
        search_preview = search_text[:50] + "..." if len(search_text) > 50 else search_text
        command_label = f"edit {path} (search: {search_preview})"

        # Check auto-edit mode
        if self.chat_manager.approve_mode == "accept_edits":
            return self._execute_edit(arguments, command_label)

        return self._handle_edit_confirmation(arguments, command_label, thinking_indicator)

    def _execute_edit(self, arguments, command_label):
        """Execute the edit operation.

        Args:
            arguments: Edit arguments dict
            command_label: Label for the edit operation

        Returns:
            Tuple of (should_exit, tool_result)
        """
        try:
            tool_result = run_edit_file(
                arguments, self.repo_root, self.console,
                self.debug_mode, self.gitignore_spec
            )
        except FileEditError as e:
            tool_result = f"exit_code=1\n{e}"
            self.console.print(f"Edit failed: {e}", style="red")

        return False, tool_result

    def _handle_edit_confirmation(self, arguments, command_label, thinking_indicator):
        """Handle edit confirmation workflow.

        Args:
            arguments: Edit arguments dict
            command_label: Label for the edit operation
            thinking_indicator: Optional ThinkingIndicator instance

        Returns:
            Tuple of (should_exit, tool_result)
        """
        if thinking_indicator:
            thinking_indicator.pause()

        # Simple title line
        self.console.print("[cyan]───[/][bold white] Edit Confirmation [/][cyan]───[/]")

        # Create dynamic message function that updates when mode changes
        def get_prompt_message():
            from prompt_toolkit.formatted_text import HTML
            return HTML('Approve edit? (y/n/guidance):')

        # Create prompt session with key bindings and toolbar
        session = create_confirmation_prompt_session(self.chat_manager, get_prompt_message)

        user_input = session.prompt().strip().lower()
        self.console.print()

        if thinking_indicator:
            thinking_indicator.resume()

        if user_input in ("y", "yes", "approve"):
            return self._execute_edit(arguments, command_label)
        elif user_input in ("n", "no"):
            return True, f"exit_code=1\nEdit cancelled by user."
        else:
            # Guidance mode - return tool result with user guidance
            # OpenAI API requires a tool response for every tool_call_id
            return False, f"User provided guidance: {user_input}"

    def _handle_web_search(self, tool_id, arguments, thinking_indicator):
        """Handle web_search tool call.

        Args:
            tool_id: Tool call ID
            arguments: Tool arguments dict
            thinking_indicator: Optional ThinkingIndicator instance

        Returns:
            Tuple of (should_exit, tool_result)
        """
        query = arguments.get("query", "")

        if WEB_SEARCH_REQUIRE_CONFIRMATION:
            return self._handle_web_search_confirmation(arguments, query, thinking_indicator)
        else:
            return self._execute_web_search(arguments, query)

    def _execute_web_search(self, arguments, query):
        """Execute the web search.

        Args:
            arguments: Search arguments dict
            query: Search query string

        Returns:
            Tuple of (should_exit, tool_result)
        """
        # Get console respecting parallel context
        console = self._get_console()

        if console:
            console.print(f"[bold cyan]web search | {query}[/bold cyan]", highlight=False)
        try:
            tool_result = run_web_search(arguments, self.console)
            if console:
                _display_tool_feedback(f"web search | {query}", tool_result, console, indent=self.is_sub_agent, panel_updater=self.panel_updater)
        except LLMConnectionError as e:
            tool_result = f"exit_code=1\nWeb search failed: {e}"
            if self.panel_updater:
                self.panel_updater.append(f"[red]Web search failed: {e}[/red]")
            elif console:
                console.print(f"Web search failed: {e}", style="red")
            # Don't cache errors
            return False, tool_result

        return False, tool_result

    def _handle_web_search_confirmation(self, arguments, query, thinking_indicator):
        """Handle web search confirmation workflow.

        Args:
            arguments: Search arguments dict
            query: Search query string
            thinking_indicator: Optional ThinkingIndicator instance

        Returns:
            Tuple of (should_exit, tool_result)
        """
        # Note: label will be displayed by _execute_web_search

        # Simple title line
        self.console.print("[cyan]───[/][bold white] Search Confirmation [/][cyan]───[/]")

        # Create dynamic message function that updates when mode changes
        def get_prompt_message():
            from prompt_toolkit.formatted_text import HTML
            return HTML('Approve search? (y/n/guide):')

        # Create prompt session with key bindings and toolbar
        session = create_confirmation_prompt_session(self.chat_manager, get_prompt_message)

        user_input = session.prompt().strip().lower()
        self.console.print()

        if user_input in ("y", "yes", "approve"):
            return self._execute_web_search(arguments, query)
        elif user_input in ("n", "no"):
            return True, f"exit_code=1\nWeb search cancelled by user."
        else:
            # Guidance mode - return tool result with user guidance
            # OpenAI API requires a tool response for every tool_call_id
            return False, f"User provided guidance: {user_input}"

    def _calculate_line_range(self, start_line: int, end_line: int) -> int:
        """Calculate max_lines from start and end line numbers.
        
        Args:
            start_line: Starting line number (1-based, inclusive)
            end_line: Ending line number (1-based, inclusive)
            
        Returns:
            Number of lines in the range
        """
        return end_line - start_line + 1

    def _handle_sub_agent(self, tool_id, arguments, thinking_indicator):
        """Handle sub_agent tool call.

        Args:
            tool_id: Tool call ID
            arguments: Tool arguments dict
            thinking_indicator: Optional ThinkingIndicator instance

        Returns:
            Tuple of (should_exit, tool_result)
        """
        query = arguments.get("query", "")
        if not query or not isinstance(query, str) or not query.strip():
            return False, "exit_code=1\nsub_agent requires a non-empty 'query' argument."

        if thinking_indicator:
            thinking_indicator.pause()

        try:
            from core.sub_agent import run_sub_agent

            # Use live panel for streaming tool output
            with SubAgentPanel(query, self.console) as panel:
                sub_agent_data = run_sub_agent(
                    task_query=query,
                    repo_root=self.repo_root,
                    rg_exe_path=self.rg_exe_path,
                    console=self.console,
                    panel_updater=panel,
                )

                # Check for errors
                if sub_agent_data.get('error'):
                    panel.set_error(sub_agent_data['error'])
                    return False, f"exit_code=1\n{sub_agent_data['error']}"

                # Track usage for billing
                usage = sub_agent_data.get('usage', {})
                if usage:
                    self.chat_manager.token_tracker.add_usage(usage)
                    # Mark panel as complete with token info
                    panel.set_complete({
                        'prompt': usage.get('prompt_tokens', 0),
                        'completion': usage.get('completion_tokens', 0),
                        'total': usage.get('prompt_tokens', 0) + usage.get('completion_tokens', 0)
                    })

                # Display sub-agent result summary (NOT shown to user, used for context)
                raw_result = sub_agent_data.get('result', '')

                # --- PARSE AND INJECT FILES ---

                injected_files_content = []

                # Regex to find explicit citation patterns (bracketed notation only for safety)
                # Matches: - [src/main.py] (lines 10-20)
                # Matches: - [src/utils.py] (full)
                # Matches: lines 10-20 in [src/main.py]
                # Matches: [src/main.py]:10-20
                # Matches: [src/main.py]:10 (single line)
                # Matches: [src/main.py] (full)
                # NOTE: Only bracketed formats are accepted to prevent false positives in code/comments
                import re
                file_pattern = re.compile(
                    r"(?:-\s+\[(.*?)\]\s+\((?:lines\s+)?(\d+)-(\d+)(?:\s*lines)?|full)\)|"  # - [file] (N-M) or (full)
                    r"(?:lines\s+(\d+)-(\d+)\s+in\s+\[(.*?)\])|"  # lines N-M in [file]
                    r"(?:\[(.*?)\]:(\d+)-(\d+))|"  # [file]:N-M
                    r"(?:\[(.*?)\]:(\d+))|"  # [file]:N (single line)
                    r"(?:\[(.*?)\](?![:(]))"  # [file] (no line numbers, read full)
                )

                for line in raw_result.split('\n'):
                    match = file_pattern.search(line)
                    if match:
                        # Handle multiple possible match groups from regex patterns
                        # Pattern 1: - [file] (N-M) or (full) -> groups: (1=file, 2=N, 3=M)
                        # Pattern 2: lines N-M in [file] -> groups: (4=start, 5=end, 6=file)
                        # Pattern 3: [file]:N-M -> groups: (7=file, 8=N, 9=M)
                        # Pattern 4: [file]:N (single line) -> groups: (10=file, 11=N)
                        # Pattern 5: [file] (full) -> groups: (12=file)

                        # Extract path and range from whichever pattern matched
                        if match.group(1):
                            # Pattern 1: - [file] (N-M) or (full)
                            rel_path = match.group(1).strip()
                            if match.group(2) and match.group(3):
                                # It's a range N-M
                                start_line = int(match.group(2))
                                end_line = int(match.group(3))
                                max_lines = self._calculate_line_range(start_line, end_line)
                            else:
                                # It's "full"
                                start_line = 1
                                max_lines = None
                        elif match.group(4) and match.group(5) and match.group(6):
                            # Pattern 2: lines N-M in [file]
                            start_line = int(match.group(4))
                            end_line = int(match.group(5))
                            rel_path = match.group(6).strip()
                            max_lines = self._calculate_line_range(start_line, end_line)
                        elif match.group(7) and match.group(8) and match.group(9):
                            # Pattern 3: [file]:N-M
                            rel_path = match.group(7).strip()
                            start_line = int(match.group(8))
                            end_line = int(match.group(9))
                            max_lines = self._calculate_line_range(start_line, end_line)
                        elif match.group(10) and match.group(11):
                            # Pattern 4: [file]:N (single line)
                            rel_path = match.group(10).strip()
                            start_line = int(match.group(11))
                            max_lines = 1
                        elif match.group(12):
                            # Pattern 5: [file] (full)
                            rel_path = match.group(12).strip()
                            start_line = 1
                            max_lines = None
                        else:
                            continue  # No valid match

                        try:
                            # Import read_file locally to bypass duplicate check for sub-agent
                            from utils.tools.file_reader import read_file as read_file_with_bypass

                            tool_result = read_file_with_bypass(
                                rel_path,
                                self.repo_root,
                                max_lines=max_lines,
                                start_line=start_line,
                                gitignore_spec=self.gitignore_spec,
                            )

                            exit_code = _get_exit_code(tool_result)
                            if exit_code is not None and exit_code != 0:
                                injected_files_content.append(f"### {rel_path} (Blocked or unavailable)")
                                injected_files_content.append(tool_result.strip())
                                injected_files_content.append("")
                                continue

                            # Add to injected content (strip metadata line from formatted tool result)
                            content_lines = tool_result.splitlines()[1:] if isinstance(tool_result, str) else []
                            content = "\n".join(content_lines).rstrip()

                            # Parse actual lines_read and start_line from metadata to match main agent display format
                            first_line = tool_result.split('\n')[0]
                            lines_read_match = re.search(r'lines_read=(\d+)', first_line)
                            start_line_match = re.search(r'start_line=(\d+)', first_line)
                            
                            if lines_read_match:
                                actual_lines_read = int(lines_read_match.group(1))
                                actual_start_line = int(start_line_match.group(1)) if start_line_match else start_line
                                
                                if actual_start_line > 1:
                                    end_line = actual_start_line + actual_lines_read - 1
                                    header_info = f"lines {actual_start_line}-{end_line} ({actual_lines_read} line{'s' if actual_lines_read != 1 else ''})"
                                else:
                                    header_info = f"lines {actual_start_line}-{actual_lines_read} ({actual_lines_read} line{'s' if actual_lines_read != 1 else ''})"
                            else:
                                header_info = "full"
                            injected_files_content.append(f"### {rel_path} ({header_info})")
                            injected_files_content.append("```")
                            injected_files_content.append(content)
                            injected_files_content.append("```\n")

                        except Exception as e:
                            injected_files_content.append(f"### {rel_path} (Error reading file: {e})")

                # Combine raw result with injected content
                if injected_files_content:
                    final_result = raw_result + "\n\n## Injected File Contents\n\n" + "\n".join(injected_files_content)
                else:
                    final_result = raw_result

                # Return result WITHOUT displaying to user (panel already handled display)
                return False, final_result

        except Exception as e:
            # Show error in panel
            with SubAgentPanel(query, self.console) as panel:
                panel.set_error(f"Sub-agent failed: {e}")
            return False, f"exit_code=1\nSub-agent failed: {e}"
        finally:
            if thinking_indicator:
                thinking_indicator.resume()


def agentic_answer(chat_manager, user_input, console, repo_root, rg_exe_path, debug_mode, thinking_indicator=None, pre_tool_planning_enabled=False):
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
        pre_tool_planning_enabled: If True, enable pre-tool planning step
    """
    orchestrator = AgenticOrchestrator(
        chat_manager=chat_manager,
        repo_root=repo_root,
        rg_exe_path=rg_exe_path,
        console=console,
        debug_mode=debug_mode,
        pre_tool_planning_enabled=pre_tool_planning_enabled,
    )
    orchestrator.run(user_input, thinking_indicator)


