"""Tool result display functions for the agentic loop."""

import re
import textwrap
from pathlib import Path
from typing import Optional

from rich.syntax import Syntax

from utils.settings import MAX_COMMAND_OUTPUT_LINES, MonokaiDarkBGStyle
from utils.result_parsers import extract_exit_code, extract_all_metadata, extract_multiple_metadata
from tools.search_plugins import HEADER_MATCHES, HEADER_ALL
from tools.task_list import _format_task_list, _strip_rich_markup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def vault_root_str() -> Optional[str]:
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


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# File extension to Pygments lexer name mapping for syntax highlighting
LEXER_MAP = {
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


# ---------------------------------------------------------------------------
# Label builders
# ---------------------------------------------------------------------------

def strip_leading_task_list_echo(content, task_list, title=None):
    """Remove a leading echoed task list from assistant content.

    Some models copy the task list tool output into the final response, which
    causes duplicate task list rendering in the CLI.
    """
    if not content or not isinstance(content, str) or not task_list:
        return content or ""

    expected = _strip_rich_markup(_format_task_list(task_list, title)).strip()
    if not expected:
        return content

    trimmed = content.lstrip()
    if trimmed.startswith(expected):
        remainder = trimmed[len(expected):]
        return remainder.lstrip("\n").lstrip()

    return content


def build_read_file_label(path, start_line=None, max_lines=None, with_colon=False):
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


def build_tool_label(function_name, arguments):
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
        return build_read_file_label(path, with_colon=True)
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


# ---------------------------------------------------------------------------
# Tool-specific feedback handlers
# ---------------------------------------------------------------------------

def handle_create_file_feedback(tool_result, console, panel_updater):
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
        lexer_name = LEXER_MAP.get(file_ext.lower(), 'text')

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


def handle_list_directory_feedback(tool_result, console, panel_updater):
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


def handle_search_plugins_feedback(tool_result, console, panel_updater):
    """Handle feedback for search_plugins tool.

    Display compact capability results in the same style as other tool output.
    """
    lines = tool_result.split('\n')
    prefix = "╰─ " if not panel_updater else ""

    query = ""
    capabilities = []
    in_capability_section = False

    i = 0
    while i < len(lines):
        raw_line = lines[i]
        line = raw_line.strip()

        if line.startswith(HEADER_MATCHES):
            query = line.replace(HEADER_MATCHES, "", 1)
            in_capability_section = True
            i += 1
            continue

        if line == HEADER_ALL:
            query = line
            in_capability_section = True
            i += 1
            continue

        if not in_capability_section:
            i += 1
            continue

        if line.startswith("Results: ") or line.startswith("Total: ") or not line:
            i += 1
            continue

        if line.startswith("Activated plugins:") or line.startswith("Loaded skills:") or line.startswith("Load issues:"):
            break

        item_match = re.match(r'^- (.+)$', line)
        if item_match:
            item = {
                "name": item_match.group(1),
                "type": "",
                "status": "",
                "summary": "",
                "tags": "",
            }
            i += 1
            while i < len(lines):
                raw_detail_line = lines[i]
                stripped = raw_detail_line.strip()
                if not stripped:
                    i += 1
                    continue
                if re.match(r'^- (.+)$', stripped):
                    break
                if stripped.startswith("Activated plugins:") or stripped.startswith("Loaded skills:") or stripped.startswith("Load issues:"):
                    break
                if stripped.startswith("type: "):
                    item["type"] = stripped.replace("type: ", "", 1)
                elif stripped.startswith("status: "):
                    item["status"] = stripped.replace("status: ", "", 1)
                elif stripped.startswith("summary: "):
                    item["summary"] = stripped.replace("summary: ", "", 1)
                elif stripped.startswith("tags: "):
                    item["tags"] = stripped.replace("tags: ", "", 1)
                i += 1
            capabilities.append(item)
            continue

        i += 1

    if not capabilities:
        msg_lines = [l for l in lines if l.strip() and not l.startswith("exit_code=")]
        output = prefix + "\n".join(msg_lines) if msg_lines else f"{prefix}No capability matches"
        _print_or_append(output, console, panel_updater)
        if not panel_updater:
            console.print()
        return

    def append_wrapped_detail(line_parts, text, indent, width=72):
        wrapped_lines = textwrap.wrap(text, width=width) or [text]
        line_parts.extend(f"{indent}[dim]{wrapped}[/dim]" for wrapped in wrapped_lines)

    max_display = 10
    display_capabilities = capabilities[:max_display]
    remaining = max(0, len(capabilities) - max_display)

    header = f"{query} ({len(capabilities)} match{'es' if len(capabilities) != 1 else ''})" if query else "capabilities"
    tree_lines = []

    for idx, capability in enumerate(display_capabilities):
        is_last = (idx == len(display_capabilities) - 1) and (remaining == 0)
        connector = "└─" if is_last else "├─"

        if capability["type"] == "plugin":
            meta = "[dim](plugin)[/dim]"
            if capability["status"]:
                meta += f" [dim][{capability['status']}][/dim]"
        else:
            meta = "[dim](skill)[/dim]"

        detail_indent = "   │  " if not is_last else "      "
        line_parts = [f"   {connector} {capability['name']} {meta}"]
        if capability["summary"]:
            append_wrapped_detail(line_parts, capability["summary"], detail_indent)
        if capability["tags"]:
            append_wrapped_detail(line_parts, f"tags: {capability['tags']}", detail_indent)
        tree_lines.append("\n".join(line_parts))

    if remaining > 0:
        tree_lines.append(f"   └─ ... and {remaining} more")

    output = f"{prefix}{header}\n" + "\n".join(tree_lines)
    _print_or_append(output, console, panel_updater)

    if not panel_updater:
        console.print()


def handle_execute_command_feedback(tool_result, console, panel_updater):
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


# ---------------------------------------------------------------------------
# Main display dispatcher
# ---------------------------------------------------------------------------

def display_tool_feedback(command, tool_result, console, indent=False, panel_updater=None):
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
        elif command.startswith("search_plugins"):
            tool_name = "search_plugins"
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
        exit_code = extract_exit_code(tool_result)
        if exit_code == 0 or exit_code is None:
            # Successful task list - display without exit_code line, with Rich markup parsing.
            rendered = tool_result
            if rendered.startswith("exit_code="):
                rendered = "\n".join(rendered.splitlines()[1:])
            _print_or_append(rendered.strip(), console, panel_updater, markup=True)
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
        handle_list_directory_feedback(tool_result, console, panel_updater)
        return

    # For search_plugins: display tree of found plugins
    if command.startswith("search_plugins"):
        handle_search_plugins_feedback(tool_result, console, panel_updater)
        return

    # For create_file: display preview of created file
    if command.startswith("create_file"):
        handle_create_file_feedback(tool_result, console, panel_updater)
        return

    # For execute_command: display command output with line truncation
    if command.startswith("execute_command"):
        handle_execute_command_feedback(tool_result, console, panel_updater)
        return

    # For web_search: display results count and content fetch status
    if command.startswith("web search"):
        lines = tool_result.split('\n')
        if lines:
            summary = _parse_web_search_metadata(lines[0])
            if summary:
                prefix = "╰─ " if not panel_updater else ""
                message = f"{prefix}[dim]{summary}[/dim]"
                _print_or_append(message, console, panel_updater)
        if not panel_updater:
            console.print()
        return


def _parse_web_search_metadata(first_line):
    """Parse web search metadata line into a human-readable summary.

    Args:
        first_line: The first line of web_search tool result containing metadata.

    Returns:
        str: Human-readable summary like "Found 5 results, 3 pages fetched, 1 failed (403)"
    """
    match = re.search(r'results_found=(\d+)', first_line)
    if not match:
        return ""

    count = int(match.group(1))
    if count == 0:
        return "No results found"

    parts = [f"Found {count} result{'s' if count != 1 else ''}"]

    fetched = re.search(r'pages_fetched=(\d+)', first_line)
    if fetched:
        fc = int(fetched.group(1))
        if fc > 0:
            parts.append(f"{fc} page{'s' if fc != 1 else ''} fetched")

    failed = re.search(r'pages_failed=(\d+)', first_line)
    if failed:
        f = int(failed.group(1))
        if f > 0:
            failures = re.search(r'failures=([^\s]+)', first_line)
            if failures:
                reasons = failures.group(1)
                parts.append(f"{f} failed ({reasons})")
            else:
                parts.append(f"{f} failed")

    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Panel message builder (used by SubAgentPanel.add_tool_call)
# ---------------------------------------------------------------------------

def build_panel_tool_message(tool_name, tool_result, command):
    """Build a formatted Rich markup message for a tool call in the sub-agent panel.

    This consolidates the formatting logic that was previously duplicated
    between SubAgentPanel.add_tool_call and display_tool_feedback.

    Args:
        tool_name: Name of the tool (e.g., "read_file", "rg")
        tool_result: Tool result string
        command: Optional command string for context (e.g., "read_file: path/to/file")

    Returns:
        str: Rich markup string for the panel message
    """
    if tool_result is None:
        return f"[grey]{tool_name}[/grey]"

    if tool_name == "read_file":
        path = ""
        if command:
            match = re.search(r'read_file:?\s+(.+)', command)
            if match:
                path = match.group(1).strip()

        metadata = extract_multiple_metadata(tool_result, 'lines_read', 'start_line')
        count = metadata.get('lines_read')

        if count is not None:
            start_line = metadata.get('start_line')
            suffix = f" ({count} line{'s' if count != 1 else ''})"
            if start_line and start_line > 1:
                end_line = start_line + count - 1
                suffix = f" lines {start_line}-{end_line}{suffix}"
            else:
                suffix = f" Read{suffix}"
            return f"[grey]read_file {path}[/grey]\n[dim]╰─{suffix}[/dim]"
        return f"[grey]read_file {path}[/grey]"

    if tool_name == "rg":
        pattern = ""
        if command:
            match = re.search(r'rg:?\s+(.+)', command)
            if match:
                pattern = match.group(1).strip()

        lines = tool_result.split('\n')
        if len(lines) > 1:
            metadata = extract_all_metadata(tool_result, line_index=1)
            count = metadata.get('matches') or metadata.get('files')
            if count is not None:
                label = 'matches' if 'matches' in metadata else 'files'
                if count == 0:
                    return f"[grey]rg {pattern}[/grey]\n[dim]╰─ No {label} found[/dim]"
                return f"[grey]rg {pattern}[/grey]\n[dim]╰─ Found {count} {label}[/dim]"
        elif any("exit_code=1" in line for line in lines):
            return f"[grey]rg {pattern}[/grey]\n[dim]╰─ No matches found[/dim]"

        # Fallback: if exit_code=1 but no footer
        if tool_result and "exit_code=1" in tool_result:
            return f"[grey]rg {pattern}[/grey]\n[dim]╰─ No matches found[/dim]"
        return f"[grey]rg {pattern}[/grey]"

    if tool_name == "list_directory":
        path = "."
        if command:
            match = re.search(r'list_directory:?\s+(.+)', command)
            if match:
                path = match.group(1).strip()

        lines = tool_result.split('\n')
        items_count = 0
        for line in lines:
            match = re.search(r'items_count=(\d+)', line)
            if match:
                items_count = int(match.group(1))
                break

        if items_count > 0:
            return f"[grey]list_directory {path}[/grey]\n[dim]╰─ {items_count} item{'s' if items_count != 1 else ''}[/dim]"
        return f"[grey]list_directory {path}[/grey]\n[dim]╰─ No items[/dim]"

    if tool_name == "search_plugins":
        query = ""
        query_match = re.search(r"matching '([^']+)':", tool_result or "")
        if query_match:
            query = query_match.group(1)

        # Count plugin entries
        plugin_count = len(re.findall(r'^- \*\*.*?\*\*', tool_result or "", re.MULTILINE))
        if plugin_count > 0:
            label = f"plugins matching '{query}'" if query else "plugins"
            return f"[grey]search_plugins {query}[/grey]\n[dim]╰─ {plugin_count} {label}[/dim]"
        return f"[grey]search_plugins {query}[/grey]\n[dim]╰─ No plugins found[/dim]"

    if tool_name == "web_search":
        query = ""
        if command:
            if "|" in command:
                parts = command.split(" | ", 1)
                if len(parts) > 1:
                    query = parts[1]

        lines = tool_result.split('\n')
        summary = _parse_web_search_metadata(lines[0]) if lines else ""

        if query:
            if summary:
                return f"[bold #5F9EA0]web search | {query}[/bold #5F9EA0]\n[dim]╰─ {summary}[/dim]"
            return f"[bold #5F9EA0]web search | {query}[/bold #5F9EA0]\n[dim]╰─ Search completed[/dim]"
        return f"[bold #5F9EA0]web_search[/bold #5F9EA0]\n[dim]╰─ Search completed[/dim]"

    if tool_name == "execute_command":
        cmd_display = ""
        if command:
            if command.startswith("execute_command"):
                parts = command.split(' ', 1)
                if len(parts) > 1:
                    cmd_display = parts[1]
            else:
                cmd_display = command
        if cmd_display:
            return f"[grey]{cmd_display}[/grey]\n[dim]╰─ Command executed[/dim]"
        return f"[grey]execute_command[/grey]\n[dim]╰─ Command executed[/dim]"

    if tool_name in ("create_task_list", "complete_task", "show_task_list"):
        exit_code = extract_exit_code(tool_result)
        if exit_code == 0 or exit_code is None:
            rendered = tool_result
            if rendered.startswith("exit_code="):
                rendered = "\n".join(rendered.splitlines()[1:])
            return f"[grey]{tool_name}[/grey]\n[dim]╰─ {rendered.strip()}[/dim]"
        first_two = "\n".join(tool_result.splitlines()[:2]).strip()
        return f"[grey]{tool_name}[/grey]\n[dim]╰─ {first_two or tool_result.strip()}[/dim]"

    return f"[grey]{tool_name}[/grey]"

