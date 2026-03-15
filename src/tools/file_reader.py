"""File reading operations."""

import os
from pathlib import Path
from typing import Optional, Dict, Tuple

from .helpers.base import tool
from .helpers.path_resolver import PathResolver
from .helpers.formatters import format_file_result
from . import constants


def _validate_read_path(
    path_str: str,
    repo_root: Path,
    gitignore_spec
) -> Tuple[Optional[Path], Optional[str]]:
    """Validate and resolve path for reading.

    Args:
        path_str: Path string to validate
        repo_root: Repository root directory
        gitignore_spec: Optional PathSpec for .gitignore filtering

    Returns:
        (resolved_path, error_message) - error_message is None if valid
    """
    resolver = PathResolver(repo_root=repo_root, gitignore_spec=gitignore_spec)
    return resolver.resolve_and_validate(
        path_str,
        check_gitignore=True,
        must_exist=True,
        must_be_file=True
    )


def _validate_start_line(start_line: Optional[int]) -> int:
    """Validate and normalize start_line parameter.

    Args:
        start_line: Optional 1-based starting line number

    Returns:
        Normalized start_line (1 or greater)
    """
    if start_line is None:
        return 1
    try:
        start_line = int(start_line)
    except (TypeError, ValueError):
        raise ValueError("start_line must be an integer (1-based).")
    if start_line < 1:
        start_line = 1
    return start_line


def _skip_lines(file_obj, lines_to_skip: int) -> bool:
    """Advance file_obj by lines_to_skip lines.

    Args:
        file_obj: File object to advance
        lines_to_skip: Number of lines to skip

    Returns:
        True if EOF reached early
    """
    if lines_to_skip <= 0:
        return False
    remaining = lines_to_skip
    while remaining > 0:
        if file_obj.readline() == "":
            return True
        remaining -= 1
    return False


def _read_full_file(file_path: Path, start_line: int) -> Dict[str, any]:
    """Read entire file, optionally starting from specific line.

    Args:
        file_path: Path to file to read
        start_line: 1-based starting line number

    Returns:
        dict with keys: content, lines_read, truncated=False
    """
    if start_line == 1:
        content = file_path.read_text(encoding="utf-8", errors="replace")
        lines_read = len(content.splitlines())
    else:
        lines = []
        with file_path.open("r", encoding="utf-8", errors="replace", newline=None) as f:
            eof_early = _skip_lines(f, start_line - 1)
            if not eof_early:
                lines = f.readlines()
        content = "".join(lines)
        lines_read = len(content.splitlines())

    return {"content": content, "lines_read": lines_read, "truncated": False}


def _read_partial_file(file_path: Path, start_line: int, max_lines: int) -> Dict[str, any]:
    """Read partial file content with streaming for large files.

    Args:
        file_path: Path to file to read
        start_line: 1-based starting line number
        max_lines: Maximum number of lines to read

    Returns:
        dict with keys: content, lines_read, truncated

    Strategy:
    - Stream in 8KB chunks
    - Extract complete lines as we go
    - Stop at max_lines
    - Handle pathological long lines (>10MB buffer)
    """
    lines = []
    truncated = False
    lines_read = 0
    chunk_size = constants.FILE_READ_CHUNK_SIZE
    max_buffer_size = constants.FILE_READ_MAX_BUFFER_SIZE

    # Use universal newlines so all newline types normalize to '\n' for parsing.
    with file_path.open("r", encoding="utf-8", errors="replace", newline=None) as f:
        eof_early = _skip_lines(f, start_line - 1)
        if eof_early:
            return {"content": "", "lines_read": 0, "truncated": False}

        if max_lines == 0:
            # Check if file has any content without loading it all
            if f.read(1):
                truncated = True
        else:
            # Streaming read: read in chunks, stop when we have enough lines
            buffer = ""
            eof_reached = False
            while lines_read < max_lines:
                chunk = f.read(chunk_size)
                if not chunk:  # EOF reached
                    eof_reached = True
                    break

                buffer += chunk

                parts = buffer.split("\n")
                complete_lines = len(parts) - 1
                remaining_capacity = max_lines - lines_read

                if complete_lines:
                    to_take = min(remaining_capacity, complete_lines)
                    for i in range(to_take):
                        lines.append(parts[i] + "\n")
                    lines_read += to_take

                    if to_take < complete_lines:
                        truncated = True
                        buffer = ""
                        break

                buffer = parts[-1]

                # If we've read enough lines and have leftover content, mark as truncated
                if lines_read >= max_lines:
                    if buffer:
                        truncated = True
                    break

                # Safeguard against extremely long single lines (pathological case)
                if len(buffer) > max_buffer_size:
                    lines.append(buffer[:max_buffer_size])
                    lines_read += 1
                    truncated = True
                    buffer = ""
                    break

            if eof_reached and not truncated and buffer and lines_read < max_lines:
                lines.append(buffer)
                lines_read += 1
                buffer = ""

            if lines_read >= max_lines and not truncated:
                # We may have stopped exactly at a chunk boundary; peek for more content.
                if f.read(1):
                    truncated = True

    content = "".join(lines)
    return {"content": content, "lines_read": lines_read, "truncated": truncated}


def _read_file_content(
    file_path: Path,
    start_line: int,
    max_lines: Optional[int]
) -> Dict[str, any]:
    """Read file content with optional line range.

    Args:
        file_path: Path to file to read
        start_line: 1-based starting line number
        max_lines: Optional maximum number of lines to read

    Returns:
        dict with keys: content, lines_read, truncated

    Logic:
    - If max_lines is None: call _read_full_file()
    - Else: call _read_partial_file()
    """
    if max_lines is None:
        return _read_full_file(file_path, start_line)
    return _read_partial_file(file_path, start_line, max_lines)


@tool(
    name="read_file",
    description="Read file contents using Python file reader. Use this to view a file (or a specific line range). Prefer this over rg when you already know the file path. Works on any file in the filesystem.",
    parameters={
        "type": "object",
        "properties": {
            "path_str": {"type": "string", "description": "Path to read (works anywhere on filesystem)"},
            "max_lines": {"type": "integer", "description": "Max lines to read (omit for full file)"},
            "start_line": {"type": "integer", "description": "1-based starting line number (default: 1). Use with max_lines to read a specific excerpt."}
        },
        "required": ["path_str"]
    },
    allowed_modes=["learn", "plan", "edit"]
)
def read_file(
    path_str: str,
    repo_root: Path,
    max_lines: Optional[int] = None,
    start_line: Optional[int] = None,
    gitignore_spec = None
) -> str:
    """Read a file's contents.

    Fast file reader that respects .gitignore, supports partial reads via
    max_lines/start_line, and provides consistent output format.

    Args:
        path_str: Path string to the file to read
        repo_root: Repository root directory (for path resolution)
        max_lines: Optional limit on number of lines to read
        start_line: Optional 1-based starting line number (default: 1)
        gitignore_spec: Optional PathSpec for .gitignore filtering

    Returns:
        str: Formatted result with exit_code, lines_read, and file content
    """
    try:
        # Validate path
        resolved, error = _validate_read_path(path_str, repo_root, gitignore_spec)
        if error:
            return format_file_result(
                exit_code=1,
                error=error,
                path=path_str
            )

        # Validate start_line
        try:
            start_line = _validate_start_line(start_line)
        except ValueError as e:
            return format_file_result(
                exit_code=1,
                error=str(e),
                path=str(resolved.relative_to(repo_root))
            )

        # Normalize max_lines
        if max_lines is not None and max_lines < 0:
            max_lines = 0

        # Read file content
        result = _read_file_content(resolved, start_line, max_lines)

        return format_file_result(
            exit_code=0,
            content=result["content"],
            path=str(resolved.relative_to(repo_root)),
            lines_read=result["lines_read"],
            start_line=start_line,
            truncated=result["truncated"]
        )

    except FileNotFoundError:
        return format_file_result(
            exit_code=1,
            error="File not found",
            path=path_str
        )
    except PermissionError:
        return format_file_result(
            exit_code=1,
            error="Permission denied",
            path=path_str
        )
    except Exception as e:
        return format_file_result(
            exit_code=1,
            error=str(e),
            path=path_str
        )
