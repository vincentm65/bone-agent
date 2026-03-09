"""File editing tool with core edit operations and @tool decorators."""

import os
import re
from pathlib import Path
from typing import Optional
from exceptions import PathValidationError, FileEditError
from rich.text import Text

from .helpers.base import tool
from .helpers.formatters import _build_diff, _detect_newline, _normalize_search_replace_for_newlines


_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_line_for_match(line, *, collapse_whitespace):
    line = line.rstrip("\r\n")
    if collapse_whitespace:
        return _WHITESPACE_RE.sub(" ", line).strip()
    return line.rstrip(" \t")


def _find_spans_by_line_normalization(content, search_text, *, collapse_whitespace):
    file_lines = content.splitlines(keepends=True)
    search_lines = search_text.splitlines(keepends=False)
    if not search_lines:
        return []

    normalized_search = [
        _normalize_line_for_match(line, collapse_whitespace=collapse_whitespace)
        for line in search_lines
    ]
    normalized_file = [
        _normalize_line_for_match(line, collapse_whitespace=collapse_whitespace)
        for line in file_lines
    ]

    offsets = [0]
    for line in file_lines:
        offsets.append(offsets[-1] + len(line))

    first = normalized_search[0]
    n = len(normalized_search)
    spans = []
    for i, file_line in enumerate(normalized_file):
        if file_line != first:
            continue
        if normalized_file[i:i + n] == normalized_search:
            spans.append((offsets[i], offsets[i + n]))
    return spans


def _build_whitespace_insensitive_pattern(search_text):
    parts = []
    i = 0
    while i < len(search_text):
        ch = search_text[i]
        if ch.isspace():
            while i < len(search_text) and search_text[i].isspace():
                i += 1
            parts.append(r"\s+")
            continue
        parts.append(re.escape(ch))
        i += 1
    return re.compile("".join(parts))


def _build_fully_whitespace_agnostic_pattern(search_text):
    parts = []
    for i, ch in enumerate(search_text):
        if ch.isspace():
            continue
        parts.append(re.escape(ch))
        if i != len(search_text) - 1:
            parts.append(r"\s*")
    return re.compile("".join(parts))


def _find_unique_span_with_fallbacks(content, search_text):
    count = content.count(search_text)
    if count == 1:
        start = content.index(search_text)
        return (start, start + len(search_text))
    if count > 1:
        raise FileEditError(
            f"Search text appears {count} times in file (must be unique)",
            details={"count": count, "hint": "Add more surrounding context to make it unique"}
        )

    if "\n" in search_text or "\r" in search_text:
        spans = _find_spans_by_line_normalization(
            content, search_text, collapse_whitespace=False
        )
        if len(spans) == 1:
            return spans[0]
        if len(spans) > 1:
            raise FileEditError(
                f"Search text appears {len(spans)} times in file (must be unique)",
                details={"count": len(spans), "hint": "Add more surrounding context to make it unique"}
            )

        spans = _find_spans_by_line_normalization(
            content, search_text, collapse_whitespace=True
        )
        if len(spans) == 1:
            return spans[0]
        if len(spans) > 1:
            raise FileEditError(
                f"Search text appears {len(spans)} times in file (must be unique)",
                details={"count": len(spans), "hint": "Add more surrounding context to make it unique"}
            )

    pattern = _build_whitespace_insensitive_pattern(search_text)
    matches = list(pattern.finditer(content))
    if len(matches) == 1:
        return matches[0].span()
    if len(matches) > 1:
        raise FileEditError(
            f"Search text appears {len(matches)} times in file (must be unique)",
            details={"count": len(matches), "hint": "Add more surrounding context to make it unique"}
        )

    if not any(ch.isspace() for ch in search_text):
        pattern = _build_fully_whitespace_agnostic_pattern(search_text)
        matches = list(pattern.finditer(content))
        if len(matches) == 1:
            return matches[0].span()
        if len(matches) > 1:
            raise FileEditError(
                f"Search text appears {len(matches)} times in file (must be unique)",
                details={"count": len(matches), "hint": "Add more surrounding context to make it unique"}
            )
    return None


def _resolve_repo_path(path_str, repo_root, gitignore_spec=None):
    """Resolve and validate a path for editing.

    Args:
        path_str: Path string to resolve
        repo_root: Repository root directory
        gitignore_spec: Optional pathspec.PathSpec for .gitignore filtering

    Returns:
        Resolved Path object

    Raises:
        PathValidationError: If path is invalid or blocked by .gitignore
    """
    raw_path = Path(path_str)
    if not raw_path.is_absolute():
        raw_path = repo_root / raw_path
    resolved = raw_path.resolve()

    # Check .gitignore (only applies to paths within repo)
    if gitignore_spec is not None:
        from utils.gitignore_filter import is_path_ignored, format_gitignore_error

        is_ignored, matched_pattern = is_path_ignored(
            resolved, repo_root, gitignore_spec
        )
        if is_ignored:
            # Create descriptive error
            error_msg = format_gitignore_error(resolved, repo_root, matched_pattern)
            raise PathValidationError(
                f"Path blocked by .gitignore: {error_msg}",
                details={"path": str(resolved), "pattern": matched_pattern}
            )

    return resolved


def _prepare_edit(arguments, repo_root, gitignore_spec=None) -> tuple[str, dict]:
    """Prepare edit operation with validation.

    Args:
        arguments: Edit arguments dict
        repo_root: Repository root
        gitignore_spec: Optional PathSpec for .gitignore filtering

    Returns:
        Tuple of (status_string, payload_dict)

    Raises:
        PathValidationError: If path is invalid or blocked by .gitignore
        FileEditError: If file cannot be read or edit is invalid
    """
    path = arguments.get("path")
    if not path or not isinstance(path, str) or not path.strip():
        raise FileEditError("Missing or invalid 'path' parameter")

    # Use updated _resolve_repo_path with gitignore checking
    try:
        file_path = _resolve_repo_path(path, repo_root, gitignore_spec)
    except PathValidationError as e:
        # Re-raise with additional context
        raise FileEditError(str(e), details=e.details)

    if not file_path.exists():
        raise FileEditError(
            f"File not found",
            details={"path": str(file_path)}
        )

    search = arguments.get("search")
    replace = arguments.get("replace")

    if search is None:
        raise FileEditError("'search' parameter is required")
    if replace is None:
        raise FileEditError("'replace' parameter is required")
    if not isinstance(search, str):
        raise FileEditError("'search' must be a string")
    if not isinstance(replace, str):
        raise FileEditError("'replace' must be a string")
    if search == "":
        raise FileEditError("'search' must be non-empty")

    try:
        with file_path.open("r", encoding="utf-8", newline="") as f:
            original_content = f.read()
    except Exception as e:
        raise FileEditError(
            f"Failed to read file",
            details={"path": str(file_path), "original_error": str(e)}
        )

    file_newline = _detect_newline(original_content)
    search, replace, _ = _normalize_search_replace_for_newlines(
        search, replace, file_newline
    )

    search_span = _find_unique_span_with_fallbacks(original_content, search)
    if search_span is None:
        search_preview = search[:200] + "..." if len(search) > 200 else search
        raise FileEditError(
            "Search text not found in file",
            details={
                "search_preview": search_preview,
                "hint": "Try adding more surrounding context (including nearby lines) to disambiguate whitespace/indentation differences."
            }
        )

    context_lines = arguments.get("context_lines", 3)
    if not isinstance(context_lines, int) or context_lines < 0:
        context_lines = 3

    return "exit_code=0", {
        "file_path": file_path,
        "original_content": original_content,
        "search_span": search_span,
        "replace": replace,
        "context_lines": context_lines,
    }


def preview_edit_file(arguments, repo_root, gitignore_spec=None) -> tuple[str, Text]:
    """Build a line-numbered diff preview without writing changes.

    Returns:
        Tuple of (status_string, diff_text)

    Raises:
        FileEditError: If edit validation fails
    """
    status, payload = _prepare_edit(arguments, repo_root, gitignore_spec)

    start, end = payload["search_span"]
    new_content = (
        payload["original_content"][:start]
        + payload["replace"]
        + payload["original_content"][end:]
    )
    diff_text = _build_diff(
        payload["original_content"],
        new_content,
        payload["file_path"],
        payload["context_lines"],
        show_header=True,
        repo_root=repo_root,
    )
    return "exit_code=0", diff_text


def run_edit_file(arguments, repo_root, console, gitignore_spec=None) -> str | Text:
    """Apply search/replace edit to a file.

    Returns:
        Rich Text with diff for success, str with exit_code for errors
    """
    try:
        status, payload = _prepare_edit(arguments, repo_root, gitignore_spec)

        start, end = payload["search_span"]
        new_content = (
            payload["original_content"][:start]
            + payload["replace"]
            + payload["original_content"][end:]
        )

        # Generate diff for preview
        diff_text = _build_diff(
            payload["original_content"],
            new_content,
            payload["file_path"],
            payload["context_lines"],
        )

        # Write to file
        try:
            with payload["file_path"].open("w", encoding="utf-8", newline="") as f:
                f.write(new_content)
        except Exception as e:
            raise FileEditError(
                f"Failed to write file",
                details={"path": str(payload["file_path"]), "original_error": str(e)}
            )

        # Success - return Rich Text object with styled diff (no exit_code prefix)
        result = Text()
        result.append(diff_text)
        result.append("\n")
        return result

    except FileEditError as e:
        # Return formatted error string for backward compatibility
        error_msg = str(e)
        if e.details:
            details_str = "\n".join(f"  {k}: {v}" for k, v in e.details.items())
            return f"exit_code=1\n{error_msg}\n{details_str}\n\n"
        return f"exit_code=1\n{error_msg}\n\n"
    except Exception as exc:
        return f"exit_code=1\n{exc}\n\n"


# =============================================================================
# @tool decorated functions
# =============================================================================

@tool(
    name="edit_file",
    description="Apply search/replace edit to file. Search text must appear exactly once. Works on any file in the filesystem.",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to edit (works anywhere on filesystem)"
            },
            "search": {
                "type": "string",
                "description": "Exact text to find. Must be unique. Include context. Multi-line supported."
            },
            "replace": {
                "type": "string",
                "description": "Replacement text. Multi-line supported."
            },
            "context_lines": {
                "type": "integer",
                "description": "Context lines in diff (default: 3)"
            },
            "reason": {
                "type": "string",
                "description": "Brief explanation of why this edit is needed (shown during confirmation)"
            }
        },
        "required": ["path", "search", "replace"]
    },
    allowed_modes=["edit", "plan", "learn"],
    requires_approval=True
)
def edit_file(
    path: str,
    search: str,
    replace: str,
    repo_root: Path,
    console,
    chat_manager,
    gitignore_spec = None,
    context_lines: int = 3
) -> str | Text:
    """Apply search/replace edit to a file.

    Args:
        path: Path to the file to edit
        search: Exact text to find (must be unique)
        replace: Replacement text
        repo_root: Repository root directory (injected by context)
        console: Rich console for output (injected by context)
        chat_manager: ChatManager instance (injected by context)
        gitignore_spec: PathSpec for .gitignore filtering (injected by context)
        context_lines: Number of context lines in diff

    Returns:
        Edit result with diff
    """
    # Check if edit_file is disabled in plan mode
    interaction_mode = getattr(chat_manager, 'interaction_mode', 'edit')
    if interaction_mode == "plan":
        return "exit_code=1\nedit_file is disabled in plan mode. Focus on theoretical outlines and provide a summary of changes at the end."

    # Validate path doesn't contain JSON-like syntax or invalid characters
    invalid_chars = '[]{}"\n\r\t'
    if any(char in path for char in invalid_chars):
        return f"exit_code=1\nedit_file 'path' contains invalid characters. Got: {path}"

    # Prepare arguments
    arguments = {
        "path": path,
        "search": search,
        "replace": replace,
        "context_lines": context_lines,
    }

    # Preview edit (confirmation workflow handled by orchestrator)
    try:
        preview_status, preview_diff = preview_edit_file(arguments, repo_root, gitignore_spec)
        if preview_status != "exit_code=0":
            return preview_status

        # Build a Rich Text object with diff only (exit_code is for agent, not user display)
        result = Text()
        result.append(preview_diff)
        return result

    except FileEditError as e:
        return f"exit_code=1\n{e}"
    except Exception as e:
        return f"exit_code=1\nEdit failed: {str(e)}"


def _execute_edit_file(
	path: str,
	search: str,
	replace: str,
	repo_root: Path,
	console,
	gitignore_spec = None,
	context_lines: int = 3
) -> str | Text:
	"""Execute a confirmed edit operation (internal function).

	Called after user confirmation to actually apply the edit.
	The main edit_file tool generates the preview first.

	Args:
		path: Path to the file to edit
		search: Exact text to find (must be unique)
		replace: Replacement text
		repo_root: Repository root directory
		console: Rich console for output
		gitignore_spec: PathSpec for .gitignore filtering
		context_lines: Number of context lines in diff

	Returns:
		Edit result with diff (Rich Text for success, str with exit_code for errors)
	"""
	arguments = {
		"path": path,
		"search": search,
		"replace": replace,
		"context_lines": context_lines,
	}

	try:
		return run_edit_file(arguments, repo_root, console, gitignore_spec)
	except FileEditError as e:
		return f"exit_code=1\n{e}"
	except Exception as e:
		return f"exit_code=1\nEdit failed: {str(e)}"