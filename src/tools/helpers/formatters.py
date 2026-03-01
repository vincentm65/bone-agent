"""Result formatting utilities for tool output and diffs."""

import os
import re
import difflib
from rich.text import Text


def _detect_newline(text):
    """Detect the newline character used in text."""
    if "\r\n" in text:
        return "\r\n"
    if "\n" in text:
        return "\n"
    return os.linesep


def _colorize_numbered_lines(lines, file_path=None):
    """Apply color highlighting to diff lines.

    - Removed lines (-): bold white text on red background, full width
    - Added lines (+): bold white text on green background, full width
    - Unchanged lines: dim grey text

    Returns:
        Rich Text object with styled content
    """
    # Get terminal width with fallback for non-TTY environments
    try:
        terminal_width = os.get_terminal_size().columns
    except (OSError, AttributeError):
        terminal_width = 80  # Fallback default

    result = Text()
    for line in lines:
        # Check the sign character (7th character, index 6)
        # Format: "   5 - text" or "   6 + text" or "   7   text"
        # Where indices 0-4 are line number, 5 is space, 6 is sign
        if len(line) >= 7:
            sign = line[6]

            if sign == "-":
                # Removed line - red background
                padded = line.ljust(terminal_width)
                result.append(padded, style="on #870101")
            elif sign == "+":
                # Added line - green background
                padded = line.ljust(terminal_width)
                result.append(padded, style="on #005f00")
            else:
                # Unchanged line - dim grey
                result.append(line, style="dim")
        else:
            result.append(line)

        result.append("\n")

    return result


def _build_numbered_diff_lines(original_content, new_content, context_lines):
    """Build numbered diff lines from original and new content."""
    if original_content == new_content:
        return [], 0, 0

    diff_lines = list(difflib.unified_diff(
        original_content.splitlines(keepends=False),
        new_content.splitlines(keepends=False),
        fromfile="old",
        tofile="new",
        n=context_lines,
        lineterm="",
    ))

    hunk_re = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    old_line = None
    new_line = None
    removed = 0
    added = 0
    formatted_lines = []

    for line in diff_lines:
        if line.startswith("--- ") or line.startswith("+++ "):
            continue
        if line.startswith("@@ "):
            match = hunk_re.match(line)
            if match:
                old_line = int(match.group(1))
                new_line = int(match.group(3))
            continue

        if old_line is None or new_line is None:
            continue

        sign = line[:1]
        text = line[1:]
        if sign == " ":
            line_no = old_line
            old_line += 1
            new_line += 1
        elif sign == "-":
            line_no = old_line
            old_line += 1
            removed += 1
        elif sign == "+":
            line_no = new_line
            new_line += 1
            added += 1
        else:
            continue

        formatted_lines.append(f"{line_no:>5} {sign} {text}")

    return formatted_lines, added, removed


def _build_diff(
    original_content,
    new_content,
    file_path,
    context_lines,
    show_header=False,
    repo_root=None
):
    """Build a diff with optional header or summary line.

    Args:
        original_content: Original file content
        new_content: Modified file content
        file_path: Path to the file being edited
        context_lines: Number of context lines for diff
        show_header: If True, show filename header (used in preview)
        repo_root: Required when show_header=True to compute relative path

    Returns:
        Rich Text object with styled diff and optional header/summary
    """
    formatted_lines, added, removed = _build_numbered_diff_lines(
        original_content, new_content, context_lines,
    )

    # Build header or summary based on mode
    if show_header:
        try:
            rel_path = file_path.relative_to(repo_root)
        except (ValueError, TypeError):
            rel_path = file_path
        header = f"{rel_path} | -{removed} | +{added}"
    else:
        header = None
        summary = f"Changes: +{added}, -{removed}"

    # Handle empty case
    if not formatted_lines:
        result = Text()
        if show_header:
            result.append(f"\n{header}\n")
            result.append("(no changes)\n", style="dim")
        else:
            result.append("(no changes)\n", style="dim")
        return result

    # Get colored diff as Text object
    diff_text = _colorize_numbered_lines(formatted_lines, file_path)

    # Build output based on mode
    result = Text()
    if show_header:
        result.append(f"\n{header}\n")
        result.append(diff_text)
    else:
        result.append(diff_text)
        result.append(f"{summary}\n")

    return result


def _normalize_search_replace_for_newlines(search, replace, newline):
    """Normalize search/replace text to match file's newline characters."""
    if newline == "\n":
        return search, replace, False
    if "\n" not in search or "\r\n" in search:
        return search, replace, False
    normalized_search = search.replace("\n", newline)
    normalized_replace = replace.replace("\n", newline)
    return normalized_search, normalized_replace, True


def format_tool_result(result, command=None, is_rg=False, debug_mode=False):
    """Format subprocess result for model consumption.

    Args:
        result: subprocess.CompletedProcess result
        command: The command that was executed (for display and mode detection)
        is_rg: Whether this was an rg command (affects empty output and counting)
        debug_mode: If True, show full output; if False, show summary only

    Returns:
        str: Formatted result with exit code
    """
    output = (result.stdout or "") + (result.stderr or "")
    output = output.strip()

    if not output:
        if is_rg and result.returncode == 1:
            output = "no matches found"
        else:
            output = "(no output)"

    # For rg commands, apply smart truncation to prevent context explosion
    if is_rg:
        label = "files" if command and "--files-with-matches" in command.lower() else "matches"
        MAX_LINES = 100  # Maximum lines of output to show

        # Exit code 0: found matches, Exit code 1: no matches
        if result.returncode == 1:
            # No matches found - rg returns 1 in this case
            count = 0
        elif result.returncode == 0:
            # Count actual matches (lines with ':number:' pattern), not context lines
            if "--files-with-matches" in (command or "").lower():
                # files-with-matches mode: count lines (each line is a file)
                lines = [line for line in output.splitlines() if line.strip()]
                count = len(lines)
            else:
                # Normal mode: count match lines by finding ':number:' pattern
                # Match format 1: path:line:content (colon before line number)
                # Match format 2: line:content (when searching single file)
                # Context format: path-line:content (hyphen before line number)
                # Try both patterns - check for path:line:content first, then line:content at start
                path_line_matches = re.findall(r':\d+:', output)
                if path_line_matches:
                    count = len(path_line_matches)
                else:
                    # Single file search: count lines starting with line number
                    count = len(re.findall(r'^\d+:', output, re.MULTILINE))
        else:
            # Error occurred (exit code 2 or higher)
            count = 0

        # Handle no matches
        if result.returncode == 1:
            return f"exit_code={result.returncode}\n{label}=0\nNo matches found\n\n"
        elif count == 0:
            # Exit code 0 but no output - unusual but possible
            return f"exit_code={result.returncode}\n{output}\n\n"

        # Truncate output if it exceeds MAX_LINES
        output_lines = output.splitlines()
        if len(output_lines) > MAX_LINES:
            truncated = "\n".join(output_lines[:MAX_LINES])
            omitted = len(output_lines) - MAX_LINES
            output = f"{truncated}\n\n... ({omitted} more {label} truncated)"
        else:
            output = "\n".join(output_lines)

        return f"exit_code={result.returncode}\n{label}={count}\n{output}\n\n"

    return f"exit_code={result.returncode}\n{output}\n\n"


def format_file_result(exit_code, content=None, error=None, path=None,
                       lines_read=None, start_line=None, truncated=False, items_count=None,
                       truncation_info=None):
    """Format file operation result for model consumption.

    Args:
        exit_code: Exit code (0 for success, 1 for error)
        content: Optional content string (for successful reads)
        error: Optional error message (for failures)
        path: Path to the file/directory
        lines_read: Number of lines read (for file reads)
        start_line: 1-based starting line number for file reads
        truncated: Whether content was truncated (for file reads)
        items_count: Number of items (for directory listings)
        truncation_info: Optional dict with truncation metadata (total, shown, omitted)

    Returns:
        str: Formatted result with exit code and metadata
    """
    metadata_parts = [f"exit_code={exit_code}"]

    if path is not None:
        metadata_parts.append(f"path={path}")

    if lines_read is not None:
        metadata_parts.append(f"lines_read={lines_read}")

    if start_line is not None:
        metadata_parts.append(f"start_line={start_line}")

    if truncated:
        metadata_parts.append("truncated=true")
        if truncation_info:
            metadata_parts.append(
                f"truncation_info=total:{truncation_info['total']},"
                f"shown:{truncation_info['shown']},"
                f"omitted:{truncation_info['omitted']}"
            )

    if items_count is not None:
        metadata_parts.append(f"items_count={items_count}")

    metadata = " ".join(metadata_parts)

    if error:
        return f"{metadata}\nerror: {error}"

    if content is not None:
        return f"{metadata}\n{content}\n\n"

    return f"{metadata}\n\n"


# format_file_preview removed - now using Rich Syntax directly in agentic.py
