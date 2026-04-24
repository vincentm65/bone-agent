"""File editing tool with core edit operations and @tool decorators."""

import os
import re
from pathlib import Path
from typing import Optional
from exceptions import PathValidationError, FileEditError
from rich.text import Text

from .helpers.base import tool
from .helpers.path_resolver import PathResolver
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
    """Find unique span for search_text, collecting diagnostics along the way.

    Returns:
        (span_tuple, diagnostics_list) on success
        (None, diagnostics_list) on failure — diagnostics explain what was tried

    diagnostics is a list of strings describing each matching stage.
    """
    diagnostics = []

    # Stage 1: Exact byte match
    count = content.count(search_text)
    diagnostics.append(f"exact match: {count} occurrence(s)")
    if count == 1:
        start = content.index(search_text)
        return (start, start + len(search_text)), diagnostics
    if count > 1:
        raise FileEditError(
            f"Search text appears {count} times in file (must be unique)",
            details={"count": count, "hint": "Add more surrounding context to make it unique"}
        )

    # Stage 2: Line-normalized match (trailing whitespace ignored)
    if "\n" in search_text or "\r" in search_text:
        spans = _find_spans_by_line_normalization(
            content, search_text, collapse_whitespace=False
        )
        diagnostics.append(f"line-normalized (trailing ws stripped): {len(spans)} match(es)")
        if len(spans) == 1:
            return spans[0], diagnostics
        if len(spans) > 1:
            raise FileEditError(
                f"Search text appears {len(spans)} times in file (must be unique)",
                details={"count": len(spans), "hint": "Add more surrounding context to make it unique"}
            )

        # Stage 3: Line-normalized + collapsed whitespace
        spans = _find_spans_by_line_normalization(
            content, search_text, collapse_whitespace=True
        )
        diagnostics.append(f"line-normalized (ws collapsed): {len(spans)} match(es)")
        if len(spans) == 1:
            return spans[0], diagnostics
        if len(spans) > 1:
            raise FileEditError(
                f"Search text appears {len(spans)} times in file (must be unique)",
                details={"count": len(spans), "hint": "Add more surrounding context to make it unique"}
            )

    # Stage 4: Regex whitespace-insensitive pattern
    pattern = _build_whitespace_insensitive_pattern(search_text)
    matches = list(pattern.finditer(content))
    diagnostics.append(f"whitespace-insensitive regex: {len(matches)} match(es)")
    if len(matches) == 1:
        return matches[0].span(), diagnostics
    if len(matches) > 1:
        raise FileEditError(
            f"Search text appears {len(matches)} times in file (must be unique)",
            details={"count": len(matches), "hint": "Add more surrounding context to make it unique"}
        )

    # Stage 5: Fully whitespace-agnostic (only for single-line search)
    if not any(ch.isspace() for ch in search_text):
        pattern = _build_fully_whitespace_agnostic_pattern(search_text)
        matches = list(pattern.finditer(content))
        diagnostics.append(f"fully whitespace-agnostic regex: {len(matches)} match(es)")
        if len(matches) == 1:
            return matches[0].span(), diagnostics
        if len(matches) > 1:
            raise FileEditError(
                f"Search text appears {len(matches)} times in file (must be unique)",
                details={"count": len(matches), "hint": "Add more surrounding context to make it unique"}
            )

    return None, diagnostics


def _resolve_repo_path(path_str, repo_root, gitignore_spec=None, vault_root=None, skip_gitignore=False):
    """Resolve and validate a path for editing.

    This function wraps PathResolver.resolve_and_validate() for the edit tool's
    specific needs, adding file type validation.

    Args:
        path_str: Path string to resolve
        repo_root: Repository root directory
        gitignore_spec: Optional pathspec.PathSpec for .gitignore filtering
        vault_root: Optional Obsidian vault root path
        skip_gitignore: If True, skip .gitignore filtering (for memory files)

    Returns:
        Resolved Path object

    Raises:
        PathValidationError: If path is invalid or blocked by .gitignore
    """
    from pathlib import Path
    vault_path = Path(vault_root) if vault_root else None
    # Use PathResolver for centralized validation
    resolver = PathResolver(repo_root=repo_root, gitignore_spec=gitignore_spec, vault_path=vault_path)
    resolved, error = resolver.resolve_and_validate(
        path_str,
        check_gitignore=not skip_gitignore,
        must_exist=True,
        must_be_file=False,  # We'll check this separately
        enforce_boundary=True,
    )

    if error:
        raise PathValidationError(
            error,
            details={"path": path_str}
        )

    # Additional validation: path must be a file for editing
    if not resolved.is_file():
        raise PathValidationError(
            f"Path is not a file: {resolved}",
            details={"path": str(resolved)}
        )

    return resolved


def _prepare_edit(arguments, repo_root, gitignore_spec=None, vault_root=None) -> tuple[str, dict]:
    """Prepare edit operation with validation.

    Args:
        arguments: Edit arguments dict
        repo_root: Repository root
        gitignore_spec: Optional PathSpec for .gitignore filtering
        vault_root: Optional Obsidian vault root path

    Returns:
        Tuple of (status_string, payload_dict)

    Raises:
        PathValidationError: If path is invalid or blocked by .gitignore
        FileEditError: If file cannot be read or edit is invalid
    """
    path = arguments.get("path")
    if not path or not isinstance(path, str) or not path.strip():
        raise FileEditError("Missing or invalid 'path' parameter")

    # Memory files (.bone/ under repo root and user_memory.md) are auto-approved
    # writes that the system itself adds to .gitignore, so gitignore filtering
    # would block them. Must anchor to repo_root to avoid matching any .bone/ dir.
    _resolved = (repo_root / path).resolve()
    is_memory = str(_resolved).startswith(str((repo_root / ".bone").resolve()) + os.sep) or Path(path).name == "user_memory.md"

    # Resolve and validate path using PathResolver
    try:
        file_path = _resolve_repo_path(path, repo_root, gitignore_spec, vault_root=vault_root,
                                       skip_gitignore=is_memory)
    except PathValidationError as e:
        # Re-raise with additional context
        raise FileEditError(str(e), details=e.details)

    if not file_path.exists():
        # Auto-create memory files (.bone/ under repo root) with default header
        # on first write. Already auto-approved, so creation is safe.
        if is_memory:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            header = "# Project Memory\n\n" if file_path.name == "agents.md" else "# User Memory\n\n"
            file_path.write_text(header, encoding="utf-8")
        else:
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
    except UnicodeDecodeError as e:
        raise FileEditError(
            "File contains non-UTF-8 bytes and cannot be edited safely",
            details={
                "path": str(file_path),
                "encoding_error": str(e),
                "hint": "This file contains bytes that are not valid UTF-8. "
                        "Use execute_command to inspect or edit it with a tool like sed or xxd."
            }
        )
    except Exception as e:
        raise FileEditError(
            f"Failed to read file",
            details={"path": str(file_path), "original_error": str(e)}
        )

    file_newline = _detect_newline(original_content)
    search, replace, _ = _normalize_search_replace_for_newlines(
        search, replace, file_newline
    )

    search_span, match_diagnostics = _find_unique_span_with_fallbacks(original_content, search)
    if search_span is None:
        search_preview = search[:200] + "..." if len(search) > 200 else search
        diagnostics_summary = "\n".join(f"  {d}" for d in match_diagnostics)
        raise FileEditError(
            "Search text not found in file",
            details={
                "search_preview": search_preview,
                "diagnostics": diagnostics_summary,
                "hint": "Try adding more surrounding context (including nearby lines) to disambiguate whitespace/indentation differences. Check that blank-line counts in your search match the file exactly."
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


def preview_edit_file(arguments, repo_root, gitignore_spec=None, vault_root=None) -> tuple[str, Text]:
    """Build a line-numbered diff preview without writing changes.

    Returns:
        Tuple of (status_string, diff_text)

    Raises:
        FileEditError: If edit validation fails
    """
    status, payload = _prepare_edit(arguments, repo_root, gitignore_spec, vault_root=vault_root)

    start, end = payload["search_span"]
    new_content = (
        payload["original_content"][:start]
        + payload["replace"]
        + payload["original_content"][end:]
    )

    # Early exit for no-op edits (e.g. fuzzy match produced identical content)
    if new_content == payload["original_content"]:
        raise FileEditError(
            "Edit is a no-op: replacement produces identical content",
            details={"hint": "Check that your search/replace text actually differs."}
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


def run_edit_file(arguments, repo_root, console, gitignore_spec=None, vault_root=None) -> str | Text:
    """Apply search/replace edit to a file.

    Returns:
        Rich Text with diff for success, str with exit_code for errors
    """
    try:
        status, payload = _prepare_edit(arguments, repo_root, gitignore_spec, vault_root=vault_root)

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

        # Skip write for no-op edits (safety net — preview should have caught this,
        # but guards against race conditions or direct calls to run_edit_file)
        if new_content == payload["original_content"]:
            raise FileEditError(
                "Edit is a no-op: replacement produces identical content",
                details={"hint": "Check that your search/replace text actually differs."}
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
    description="Apply search/replace edit to file. Search text must appear exactly once.",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to edit"
            },
            "search": {
                "type": "string",
                "description": "Exact text to find. Must be unique. Multi-line supported."
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
                "description": "Brief explanation (shown during confirmation)"
            }
        },
        "required": ["path", "search", "replace"]
    },
    requires_approval=True,
    terminal_policy="stop"
)
def edit_file(
    path: str,
    search: str,
    replace: str,
    repo_root: Path,
    console,
    chat_manager,
    gitignore_spec = None,
    context_lines: int = 3,
    vault_root: str = None,
    reason: str = None,
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
        vault_root: Obsidian vault root path (injected by context)
        reason: Brief explanation shown during confirmation

    Returns:
        Edit result with diff
    """
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
    if reason:
        arguments["reason"] = reason

    # Preview edit (confirmation workflow handled by orchestrator)
    try:
        preview_status, preview_diff = preview_edit_file(arguments, repo_root, gitignore_spec, vault_root=vault_root)
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
	context_lines: int = 3,
	vault_root: str = None
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
		vault_root: Obsidian vault root path

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
		return run_edit_file(arguments, repo_root, console, gitignore_spec, vault_root=vault_root)
	except FileEditError as e:
		return f"exit_code=1\n{e}"
	except Exception as e:
		return f"exit_code=1\nEdit failed: {str(e)}"