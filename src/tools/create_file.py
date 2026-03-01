"""File creation operations."""

import os
from pathlib import Path
from typing import Optional, Tuple

from utils.settings import MAX_FILE_PREVIEW_LINES
from .helpers.base import tool
from .helpers.file_helpers import (
    _is_fast_ignored,
    _is_ignored_cached,
    _register_gitignore_spec,
    _is_reserved_windows_name
)
from .helpers.formatters import format_file_result


def _validate_create_path(
    path_str: str,
    repo_root: Path,
    gitignore_spec
) -> Tuple[Optional[Path], Optional[str]]:
    """Validate and resolve path for file creation.

    Args:
        path_str: Path string to validate
        repo_root: Repository root directory
        gitignore_spec: Optional PathSpec for .gitignore filtering

    Returns:
        (resolved_path, error_message) - error_message is None if valid

    Checks:
    - Windows filename validation (invalid chars, reserved names)
    - Path resolution
    - Gitignore filtering (only within repo)
    """
    try:
        # Windows validation
        if os.name == 'nt':
            invalid_chars = '<>:"|?*'
            if any(char in path_str for char in invalid_chars):
                return None, f"Filename contains invalid characters: {invalid_chars}"

            filename = Path(path_str).name
            if _is_reserved_windows_name(filename):
                return None, f"Filename is a reserved Windows device name: {filename}"

        # Resolve path
        raw_path = Path(path_str)
        if not raw_path.is_absolute():
            raw_path = repo_root / raw_path
        resolved = raw_path.resolve()

        # Check gitignore (only applies to paths within repo)
        if gitignore_spec is not None:
            if _is_fast_ignored(resolved):
                return None, f"File blocked by .gitignore: {resolved.relative_to(repo_root)}"

            spec_key = _register_gitignore_spec(gitignore_spec)
            if _is_ignored_cached(str(resolved), str(repo_root), spec_key):
                return None, f"File blocked by .gitignore: {resolved.relative_to(repo_root)}"

        return resolved, None

    except Exception as e:
        return None, str(e)


@tool(
    name="create_file",
    description="Create a new file with optional initial content. File must not exist. For small files, include content directly. Creates a preview of the written content (up to 200 lines) with syntax highlighting. Works on any path in the filesystem.",
    parameters={
        "type": "object",
        "properties": {
            "path_str": {"type": "string", "description": "Path to create (works anywhere on filesystem)"},
            "content": {"type": "string", "description": "Initial content (omit for empty file)"}
        },
        "required": ["path_str"]
    },
    allowed_modes=["edit"]
)
def create_file(
    path_str: str,
    repo_root: Path,
    content: Optional[str] = None,
    gitignore_spec = None
) -> str:
    """Create a new file with optional initial content.

    Creates a new file at the specified path, creating parent directories
    if needed. The file must not already exist. Respects .gitignore.

    Args:
        path_str: Path string to the file to create
        repo_root: Repository root directory (for path resolution)
        content: Optional initial content for the file. If omitted, creates empty file.
        gitignore_spec: Optional PathSpec for .gitignore filtering

    Returns:
        str: Formatted result with exit_code and status, including preview
    """
    try:
        # Validate path
        resolved, error = _validate_create_path(path_str, repo_root, gitignore_spec)
        if error:
            return format_file_result(exit_code=1, error=error, path=path_str)

        # Check if already exists
        if resolved.exists():
            return format_file_result(
                exit_code=1,
                error="File already exists",
                path=str(resolved.relative_to(repo_root))
            )

        # Create parent directories if needed
        parent_dir = resolved.parent
        if parent_dir != repo_root and not parent_dir.exists():
            parent_dir.mkdir(parents=True, exist_ok=True)

        # Write content or create empty file
        if content is not None:
            resolved.write_text(content, encoding="utf-8", newline="")
        else:
            content = ""
            resolved.touch()

        # Build result with content for display (truncate preview if needed)
        result_lines = []
        result_lines.append(f"exit_code=0")
        result_lines.append(f"path={resolved.relative_to(repo_root)}")
        result_lines.append(f"content=File created successfully")
        result_lines.append("")
        result_lines.append(f"=== FILE_CONTENT ===")

        # Truncate content for preview if it exceeds max lines
        if content:
            content_lines = content.splitlines(keepends=True)
            if len(content_lines) > MAX_FILE_PREVIEW_LINES:
                truncated_content = "".join(content_lines[:MAX_FILE_PREVIEW_LINES])
                omitted = len(content_lines) - MAX_FILE_PREVIEW_LINES
                result_lines.append(truncated_content)
                result_lines.append(f"\n... ({omitted} more lines omitted from preview)")
            else:
                result_lines.append(content)

        result_lines.append("=== END_FILE_CONTENT ===")

        return "\n".join(result_lines) + "\n\n"

    except PermissionError:
        return format_file_result(exit_code=1, error="Permission denied", path=path_str)
    except OSError as e:
        return format_file_result(exit_code=1, error=f"Invalid filename: {e}", path=path_str)
    except Exception as e:
        return format_file_result(exit_code=1, error=str(e), path=path_str)
