"""Result formatting utilities for tool output and diffs."""

import os
import re
import difflib
from rich.text import Text

# Import constants module to access centralized values
try:
    from ..constants import (
        DEFAULT_TERMINAL_WIDTH,
        FORMATTER_MAX_LINES,
    )
except ImportError:
    # Fallback for standalone usage
    DEFAULT_TERMINAL_WIDTH = 80
    FORMATTER_MAX_LINES = 100

# Shell output truncation (lazy import to avoid circular dependency)
_SHELL_MAX_LINES = None

def _get_shell_max_lines():
    """Get shell output line limit from settings (lazy import)."""
    global _SHELL_MAX_LINES
    if _SHELL_MAX_LINES is None:
        try:
            from utils.settings import MAX_SHELL_OUTPUT_LINES
            _SHELL_MAX_LINES = MAX_SHELL_OUTPUT_LINES
        except ImportError:
            _SHELL_MAX_LINES = 200
    return _SHELL_MAX_LINES


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
        terminal_width = DEFAULT_TERMINAL_WIDTH  # Fallback default

    # We pre-pad changed lines to exact terminal-width multiples so the
    # background color fills every visual row when long lines wrap. Rich's
    # normal Text wrapping can reflow at spaces and trim trailing padding from
    # wrapped chunks, which makes highlights stop before the terminal edge.
    # Disable Rich wrapping here and let the terminal wrap the padded text.
    result = Text(no_wrap=True, overflow="ignore")
    for line in lines:
        # Check the sign character (7th character, index 6)
        # Format: "   5 - text" or "   6 + text" or "   7   text"
        # Where indices 0-4 are line number, 5 is space, 6 is sign
        if len(line) >= 7:
            sign = line[6]

            if sign == "-":
                # Removed line - red background
                # Pad to next multiple of terminal_width so wrapped rows
                # also get full-width background highlight
                padded_width = -(-len(line) // terminal_width) * terminal_width
                padded = line.ljust(padded_width)
                result.append(padded, style="on #870101")
            elif sign == "+":
                # Added line - green background
                padded_width = -(-len(line) // terminal_width) * terminal_width
                padded = line.ljust(padded_width)
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
        result = Text(no_wrap=True, overflow="ignore")
        if show_header:
            result.append(f"\n{header}\n")
            result.append("(no changes)\n", style="dim")
        else:
            result.append("(no changes)\n", style="dim")
        return result

    # Get colored diff as Text object
    diff_text = _colorize_numbered_lines(formatted_lines, file_path)

    # Build output based on mode. Preserve the no-wrap/ignore rendering mode
    # used by _colorize_numbered_lines after appending the colored diff into
    # the wrapper Text object.
    result = Text(no_wrap=True, overflow="ignore")
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


def _is_rg_match_line(line):
    """Return True if an rg output line is a match (not context/separator).

    Match formats:
      path:line:content   (colon before line number)
      line:content        (single-file search, line starts with number)
    Context/separators are excluded (path-line:content uses hyphen, not colon).
    """
    if re.search(r':\d+:', line):
        return True
    return bool(re.match(r'^\d+:', line))


def format_tool_result(result, command=None, is_rg=False, debug_mode=False, max_matches=None):
    """Format subprocess result for model consumption.

    Args:
        result: subprocess.CompletedProcess result
        command: The command that was executed (for display and mode detection)
        is_rg: Whether this was an rg command (affects empty output and counting)
        debug_mode: If True, show full output; if False, show summary only
        max_matches: Maximum number of matches to return (0 = use line-based limit, >0 = match limit)

    Returns:
        str: Formatted result with exit code
    """
    # Number of trailing context lines to include after the last match when truncating
    _RG_TRAILING_CONTEXT_LINES = 5

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
        MAX_LINES = FORMATTER_MAX_LINES

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
                # Normal mode: count match lines (lines with ':number:' pattern), not context
                count = sum(1 for line in output.splitlines() if _is_rg_match_line(line))
        else:
            # Error occurred (exit code 2 or higher)
            count = 0

        # Handle no matches
        if result.returncode == 1:
            return f"exit_code={result.returncode}\n{label}=0\nNo matches found\n\n"
        elif count == 0:
            # Exit code 0 but no output - unusual but possible
            return f"exit_code={result.returncode}\n{output}\n\n"

        # Truncate output
        output_lines = output.splitlines()

        # Use max_matches directly if positive (>0 means match limit, 0/None means line-based fallback)
        effective_max_matches = max_matches if max_matches and max_matches > 0 else None

        if effective_max_matches is not None and "--files-with-matches" in (command or "").lower():
            # files_with_matches mode: truncate by file count (each line is a file)
            if len(output_lines) > effective_max_matches:
                truncated = "\n".join(output_lines[:effective_max_matches])
                omitted = len(output_lines) - effective_max_matches
                output = f"{truncated}\n\n... ({omitted} more {label} truncated)"
                return f"exit_code={result.returncode}\n{label}={len(output_lines)}\n{output}\n\n"
            else:
                output = "\n".join(output_lines)
                return f"exit_code={result.returncode}\n{label}={count}\n{output}\n\n"

        if effective_max_matches is not None:
            # Match-based truncation: find lines that are actual matches (contain :number:)
            # and cut after the Nth match including its trailing context
            is_match = [_is_rg_match_line(line) for line in output_lines]

            # Compute total match count once upfront
            total_matches = sum(is_match)

            # Find cutoff point after Nth match
            match_count = 0
            cutoff = len(output_lines)
            for i, match in enumerate(is_match):
                if match:
                    match_count += 1
                    if match_count >= effective_max_matches:
                        # Include up to _RG_TRAILING_CONTEXT_LINES trailing context lines after this match
                        cutoff = i + 1
                        for j in range(i + 1, min(i + 1 + _RG_TRAILING_CONTEXT_LINES, len(is_match))):
                            if is_match[j]:
                                break
                            cutoff = j + 1
                        break

            if cutoff < len(output_lines):
                truncated = "\n".join(output_lines[:cutoff])
                omitted_matches = max(0, total_matches - effective_max_matches)
                output = f"{truncated}\n\n... ({omitted_matches} more {label} truncated)"
                return f"exit_code={result.returncode}\n{label}={total_matches}\n{output}\n\n"
            else:
                output = "\n".join(output_lines)
                return f"exit_code={result.returncode}\n{label}={total_matches}\n{output}\n\n"

        # Fallback: line-based truncation
        if len(output_lines) > MAX_LINES:
            truncated = "\n".join(output_lines[:MAX_LINES])
            omitted = len(output_lines) - MAX_LINES
            output = f"{truncated}\n\n... ({omitted} more {label} truncated)"
        else:
            output = "\n".join(output_lines)

        return f"exit_code={result.returncode}\n{label}={count}\n{output}\n\n"

    # For non-rg shell commands: apply head+tail truncation
    output_lines = output.splitlines()
    max_lines = _get_shell_max_lines()

    if len(output_lines) > max_lines:
        head_count = max_lines // 2
        tail_count = max_lines - head_count
        omitted = len(output_lines) - max_lines
        head = "\n".join(output_lines[:head_count])
        tail = "\n".join(output_lines[-tail_count:])
        output = f"{head}\n\n... ({omitted} lines omitted) ...\n\n{tail}"
    else:
        output = "\n".join(output_lines)

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
