"""Result formatting utilities for tool output and diffs."""

import os
import re
import difflib
import shutil
from pathlib import Path
from pygments import highlight
from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from pygments.formatters.terminal256 import Terminal256Formatter
from pygments.util import ClassNotFound
from rich.text import Text

# Module-level lexer cache for syntax highlighting
_lexer_cache = {}


def _get_lexer_for_file(file_path):
    """Get cached lexer for file extension.

    Args:
        file_path: Path to file being highlighted

    Returns:
        Pygments lexer instance
    """
    if not file_path:
        return get_lexer_by_name('text')

    ext = Path(file_path).suffix
    if ext not in _lexer_cache:
        try:
            _lexer_cache[ext] = get_lexer_for_filename(file_path)
        except ClassNotFound:
            _lexer_cache[ext] = get_lexer_by_name('text')
    return _lexer_cache[ext]


def strip_ansi_codes(text: str) -> str:
    """Remove ANSI escape codes from text.

    Args:
        text: Text that may contain ANSI escape codes

    Returns:
        Text with ANSI codes removed
    """
    # ANSI escape sequences pattern
    ansi_pattern = re.compile(r'\x1b\[[0-9;]*m')
    return ansi_pattern.sub('', text)


def _detect_newline(text):
    """Detect the newline character used in text."""
    if "\r\n" in text:
        return "\r\n"
    if "\n" in text:
        return "\n"
    return os.linesep


def _should_color(color_mode):
    """Determine if diff output should be colorized."""
    if color_mode == "on":
        return True
    if color_mode == "off":
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    return True


def _get_bg_ansi(sign, color_mode):
    """Get ANSI background color and reset codes for diff lines."""
    if not _should_color(color_mode):
        return "", ""

    if sign == "+":
        return "\033[48;5;22m", "\033[0m"  # Dark green
    elif sign == "-":
        return "\033[48;5;52m", "\033[0m"  # Dark red
    else:
        return "", ""  # No background for context lines


def _insert_padding_at_wrap_points(text, terminal_width, prefix_width, bg_ansi):
    """Insert padding spaces at wrap points to fill gaps on all wrapped rows.
    
    This ensures background color extends through every row when text wraps,
    not just the last row.
    
    Args:
        text: ANSI-colored text (from Pygments)
        terminal_width: Terminal column width
        prefix_width: Width of line number prefix (e.g., "123 + ")
        bg_ansi: Background ANSI code to preserve after padding
        
    Returns:
        Text with padding inserted at each wrap point
    """
    # ANSI escape sequence pattern
    ansi_pattern = re.compile(r'\x1b\[[0-9;]*m')
    
    # Split text into segments (alternating: plain text, ANSI code, plain text, ...)
    parts = []
    last_end = 0
    for match in ansi_pattern.finditer(text):
        parts.append(text[last_end:match.start()])  # plain text
        parts.append(match.group(0))  # ANSI code
        last_end = match.end()
    parts.append(text[last_end:])  # remaining plain text
    
    result = []
    current_width = prefix_width
    
    for i, part in enumerate(parts):
        if i % 2 == 0:  # plain text segment
            # Process character by character
            for char in part:
                char_width = Text.from_ansi(char).cell_len
                
                # Check if adding this char would wrap
                if current_width + char_width >= terminal_width:
                    # Calculate padding needed to reach end of current row
                    padding_needed = terminal_width - current_width
                    if padding_needed > 0:
                        # Fill current row to edge with background (no reset - let it wrap)
                        result.append(bg_ansi + " " * padding_needed)
                    # Background stays active through wrap to next line
                    current_width = 0  # Reset for next row
                
                result.append(char)
                current_width += char_width
        else:  # ANSI code segment
            result.append(part)
    
    # Pad the final row to terminal edge to ensure full background coverage
    padding_needed = terminal_width - current_width
    if padding_needed > 0:
        result.append(bg_ansi + " " * padding_needed + "\x1b[0m")
    
    return ''.join(result)


def _colorize_numbered_lines(lines, color_mode, file_path=None):
    """Add ANSI color codes to numbered diff lines with background highlights and syntax highlighting."""
    # Fast-path: skip all colorization if color mode is off
    if not _should_color(color_mode):
        return lines

    # Use cached lexer for the file extension
    lexer = _get_lexer_for_file(file_path)

    colored = []

    for line in lines:
        if len(line) < 8:
            bg, rst = _get_bg_ansi(" ", color_mode)
            ansi = bg + line + rst if bg else line
            colored.append(ansi)
            continue

        sign = line[6]
        prefix = line[:7]
        code = line[8:]

        # Get background color first so we can preserve it through syntax highlighting
        bg, rst = _get_bg_ansi(sign, color_mode)

        syntax_ansi = highlight(code, lexer, Terminal256Formatter(style='monokai')).rstrip('\n')

        # Replace Pygments' reset codes with reset+reapply background
        # This prevents syntax highlighting from clearing the diff background color
        # Pygments can emit: \x1b[0m, \x1b[39m, \x1b[49m, \x1b[39;49m, \x1b[49;39m, \x1b[39;49;00m, \x1b[49;39;00m
        # Any sequence that resets background (49) needs to reapply our diff background
        if bg:
            # Match all reset sequences that clear the background
            # Pattern matches: \x1b[0m, \x1b[39m, \x1b[49m, \x1b[39;49m, \x1b[49;39m, \x1b[39;49;00m, \x1b[49;39;00m
            reset_pattern = re.compile(r'\x1b\[(?:0m|39m|49m|39;49m|49;39m|39;49;00m|49;39;00m)')
            syntax_ansi = reset_pattern.sub(lambda m: m.group(0) + bg, syntax_ansi)

        if bg:
            # Calculate visible width using Rich's Text to handle Unicode and ANSI codes
            # cell_len properly accounts for wide characters (e.g., → has width 2) and ignores ANSI codes
            prefix_width = len(prefix) + 1  # prefix + space
            code_width = Text.from_ansi(syntax_ansi).cell_len
            visible_width = prefix_width + code_width
            try:
                terminal_width = shutil.get_terminal_size(fallback=(80, 20)).columns
            except OSError:
                terminal_width = 80
            
            # Insert padding at wrap points to fill gaps on all wrapped rows
            # This ensures background color extends through every row when text wraps
            padded_code = _insert_padding_at_wrap_points(
                syntax_ansi, terminal_width, prefix_width, bg
            )
            ansi_line = bg + prefix + " " + padded_code + rst
        else:
            ansi_line = prefix + " " + syntax_ansi

        colored.append(ansi_line)

    return colored


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
    color_mode,
    show_header=False,
    repo_root=None
):
    """Build a diff with optional header or summary line.

    Args:
        original_content: Original file content
        new_content: Modified file content
        file_path: Path to the file being edited
        context_lines: Number of context lines for diff
        color_mode: Color mode for diff display ('auto', 'on', 'off')
        show_header: If True, show filename header (used in preview)
        repo_root: Required when show_header=True to compute relative path

    Returns:
        Formatted diff string with either header (preview mode) or summary (edit mode)
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
        header = f"\x1b[1m{rel_path} | -{removed} | +{added}\x1b[0m"
    else:
        header = None
        summary = f"Changes: +{added}, -{removed}"

    # Handle empty case
    if not formatted_lines:
        if show_header:
            return f"\n{header}\n(no changes)"
        return "(no changes)"

    # Validate and colorize
    if color_mode not in ("auto", "on", "off"):
        color_mode = "auto"

    colored = _colorize_numbered_lines(formatted_lines, color_mode, file_path)

    # Build output based on mode
    if show_header:
        return f"\n{header}\n" + "\n".join(colored)
    return "\n".join(colored + [summary])


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
            return f"exit_code={result.returncode}\nNo matches found\n\n"
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
