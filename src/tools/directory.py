"""Directory listing operations."""

import fnmatch
import os
from pathlib import Path
from typing import Optional, Tuple, Dict, List

from .helpers.base import tool
from .helpers.path_resolver import PathResolver
from .helpers.file_helpers import GitignoreFilter
from .helpers.formatters import format_file_result
from . import constants


def _group_items_by_directory(items, show_files, show_dirs) -> Dict:
    """Group items by their parent directory for smart truncation.

    Args:
        items: List of (kind, rel_path, size, raw_path, line_count) tuples
        show_files: Whether files are included in results
        show_dirs: Whether directories are included in results

    Returns:
        Dict mapping parent_dir -> {'dirs': [dir_items], 'files': [file_items]}
    """
    groups = {}

    for kind, rel_path, size, raw_path, line_count in items:
        # Get parent directory
        if '/' in rel_path:
            parent_dir = Path(rel_path).parent
        else:
            parent_dir = Path('.')  # Root level

        if parent_dir not in groups:
            groups[parent_dir] = {'dirs': [], 'files': []}

        if kind == 'DIR ':
            groups[parent_dir]['dirs'].append((kind, rel_path, size, raw_path, line_count))
        else:  # FILE
            groups[parent_dir]['files'].append((kind, rel_path, size, raw_path, line_count))

    return groups


def _apply_smart_truncation(items, show_files, show_dirs, hit_limit=False) -> Tuple[List, Optional[Dict]]:
    """Apply smart truncation to directory listing results.

    Truncation Strategy:
    - If total items < TRUNCATION_THRESHOLD: No truncation
    - If total items >= TRUNCATION_THRESHOLD:
      * ALL directories are shown (preserve structure)
      * Files are sampled: top MAX_FILES_PER_FOLDER per directory
      * Truncation metadata returned for message generation

    Args:
        items: List of (kind, rel_path, size, raw_path) tuples
        show_files: Whether files are included in results
        show_dirs: Whether directories are included in results
        hit_limit: Whether we hit MAX_TOTAL_ITEMS during collection

    Returns:
        Tuple of:
        - List of items after truncation
        - None if no truncation, or dict with truncation metadata
    """
    total_count = len(items)

    # Fast path: no truncation needed
    if total_count < constants.TRUNCATION_THRESHOLD:
        return items, None

    # Group items by parent directory
    groups = _group_items_by_directory(items, show_files, show_dirs)

    # Apply truncation per directory
    truncated_items = []
    total_dirs_shown = 0
    total_files_shown = 0
    total_files_omitted = 0

    # Sort directories alphabetically for consistent output
    for parent_dir in sorted(groups.keys(), key=lambda p: str(p)):
        group = groups[parent_dir]

        # Always include all directories
        if show_dirs:
            truncated_items.extend(group['dirs'])
            total_dirs_shown += len(group['dirs'])

        # Truncate files if needed
        if show_files and group['files']:
            # Sort files alphabetically and take top N
            sorted_files = sorted(group['files'], key=lambda x: x[1])  # Sort by rel_path
            files_to_show = sorted_files[:constants.MAX_FILES_PER_FOLDER]
            truncated_items.extend(files_to_show)

            total_files_shown += len(files_to_show)
            total_files_omitted += len(group['files']) - len(files_to_show)

    # Build truncation info
    truncation_info = {
        'total': total_count,
        'shown': len(truncated_items),
        'omitted': total_count - len(truncated_items),
        'dirs_shown': total_dirs_shown,
        'files_shown': total_files_shown,
        'files_omitted': total_files_omitted,
        'all_dirs_shown': True
    }

    return truncated_items, truncation_info


def _validate_directory_path(
    path_str: str,
    repo_root: Path
) -> Tuple[Optional[Path], Optional[str]]:
    """Validate and resolve path for directory listing.

    This function wraps PathResolver.resolve_and_validate() for the directory
    tool's specific needs, ensuring the path is a directory.

    Args:
        path_str: Path string to validate
        repo_root: Repository root directory

    Returns:
        (resolved_path, error_message) - error_message is None if valid
    """
    # Use PathResolver for centralized validation
    resolver = PathResolver(repo_root=repo_root, gitignore_spec=None)
    resolved, error = resolver.resolve_and_validate(
        path_str,
        check_gitignore=False,  # Directory listing shows everything
        must_exist=True,
        must_be_dir=True  # Must be a directory
    )

    if error:
        return None, error

    return resolved, None


@tool(
    name="list_directory",
    description="List directory contents using Python file lister (preferred over PowerShell). Works on any directory in the filesystem.",
    parameters={
        "type": "object",
        "properties": {
            "path_str": {"type": "string", "description": "Path to list (default: '.', works anywhere on filesystem)"},
            "recursive": {"type": "boolean", "description": "List recursively (default: false)"},
            "show_files": {"type": "boolean", "description": "Include files (default: true)"},
            "show_dirs": {"type": "boolean", "description": "Include directories (default: true)"},
            "pattern": {"type": "string", "description": "Glob pattern to filter results (e.g., \"*.py\")"}
        },
        "required": ["path_str"]
    },
    allowed_modes=["edit", "plan"]
)
def list_directory(
    path_str: str,
    repo_root: Path,
    recursive: bool = False,
    show_files: bool = True,
    show_dirs: bool = True,
    pattern: Optional[str] = None,
    gitignore_spec = None
) -> str:
    """List directory contents.

    Directory listing that respects .gitignore and shows file sizes in a
    consistent format.

    Args:
        path_str: Path string to the directory to list
        repo_root: Repository root directory (for path resolution)
        recursive: List recursively
        show_files: Include files in output
        show_dirs: Include directories in output
        pattern: Optional glob pattern to filter results (e.g., "*.py")
        gitignore_spec: Optional PathSpec for .gitignore filtering

    Returns:
        str: Formatted result with exit_code, items_count, and directory listing
    """
    try:
        # Validate path
        resolved, error = _validate_directory_path(path_str, repo_root)
        if error:
            return format_file_result(
                exit_code=1,
                error=error,
                path=path_str
            )

        def _match_pattern(rel_path: Path) -> bool:
            if not pattern:
                return True
            return fnmatch.fnmatch(rel_path.as_posix(), pattern)

        # Create gitignore filter if spec is provided
        gitignore_filter = GitignoreFilter(repo_root=repo_root, gitignore_spec=gitignore_spec) if gitignore_spec else None

        def _is_ignored(path: Path) -> bool:
            if path.name == ".gitignore":
                return True
            if ".git" in path.parts:
                return True
            if gitignore_filter is None:
                return False
            # Use GitignoreFilter for consistent behavior
            return gitignore_filter.is_ignored(path)

        # Collect items
        items = []
        base_dir = resolved
        hit_limit = False  # Track if we hit constants.MAX_TOTAL_ITEMS

        def _count_lines(file_path: Path) -> int:
            """Count lines in a file efficiently."""
            try:
                with open(file_path, 'rb') as f:
                    return sum(1 for _ in f) - 1  # Subtract 1 for last newline, but handle empty files
            except (OSError, IOError):
                return 0

        def _add_item(kind, rel_path, size_str, raw_path, line_count=None):
            nonlocal hit_limit
            if kind == "FILE" and not show_files:
                return
            if kind == "DIR " and not show_dirs:
                return
            if not _match_pattern(rel_path):
                return
            if hit_limit:
                return
            if len(items) >= constants.MAX_TOTAL_ITEMS:
                hit_limit = True
                return
            items.append((kind, str(rel_path), size_str, raw_path, line_count))

        if recursive:
            stack = [resolved]
            while stack and not hit_limit:
                current = stack.pop()
                try:
                    with os.scandir(current) as it:
                        for entry in it:
                            try:
                                if entry.is_symlink():
                                    continue
                            except OSError:
                                continue

                            is_dir = entry.is_dir(follow_symlinks=False)
                            is_file = entry.is_file(follow_symlinks=False)

                            if not is_dir and not is_file:
                                continue

                            entry_path = Path(entry.path)

                            if _is_ignored(entry_path):
                                continue

                            rel_path = entry_path.relative_to(base_dir)

                            if is_file:
                                try:
                                    size = f"{entry.stat(follow_symlinks=False).st_size:>10}"
                                    line_count = _count_lines(entry_path)
                                except OSError:
                                    size = "         ?"
                                    line_count = 0
                                _add_item("FILE", rel_path, size, entry_path, line_count)
                            else:
                                _add_item("DIR ", rel_path, "          ", entry_path, line_count=0)
                                stack.append(entry_path)
                except PermissionError:
                    continue
        else:
            with os.scandir(resolved) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                    except OSError:
                        continue

                    is_dir = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)

                    if not is_dir and not is_file:
                        continue

                    entry_path = Path(entry.path)

                    if _is_ignored(entry_path):
                        continue

                    rel_path = entry_path.relative_to(base_dir)

                    if is_file:
                        try:
                            size = f"{entry.stat(follow_symlinks=False).st_size:>10}"
                            line_count = _count_lines(entry_path)
                        except OSError:
                            size = "         ?"
                            line_count = 0
                        _add_item("FILE", rel_path, size, entry_path, line_count)
                    else:
                        _add_item("DIR ", rel_path, "          ", entry_path, line_count=0)

        # Sort: directories first, then alphabetically
        items.sort(key=lambda x: (0 if x[0] == "DIR " else 1, x[1]))

        # Format output
        if not items:
            return format_file_result(
                exit_code=0,
                content="(empty directory)",
                path=str(resolved.relative_to(repo_root)),
                items_count=0
            )

        # Apply smart truncation if needed
        truncated_items, truncation_info = _apply_smart_truncation(items, show_files, show_dirs, hit_limit)

        # Build lines with truncation message
        lines = []
        for kind, rel_path, size, _, line_count in truncated_items:
            if kind == "FILE":
                lines.append(f"{kind}  {rel_path}  {line_count:6} lines  {size} bytes")
            else:
                lines.append(f"{kind}  {rel_path}  {line_count:6} lines")

        # Add truncation message at end
        if truncation_info:
            lines.append("")
            msg = f"[{truncation_info['files_omitted']} file(s) omitted ({truncation_info['shown']} shown from {truncation_info['total']} total items)]"
            if hit_limit:
                msg += f"\n[WARNING: Listing stopped at {constants.MAX_TOTAL_ITEMS} items to prevent context overflow. Use filters or specific paths to explore further.]"
            lines.append(msg)

        content = "\n".join(lines)

        # Update truncation info to indicate if we hit the hard limit
        if truncation_info and hit_limit:
            truncation_info['hit_limit'] = True
            truncation_info['max_items'] = constants.MAX_TOTAL_ITEMS

        return format_file_result(
            exit_code=0,
            content=content,
            path=str(resolved.relative_to(repo_root)),
            items_count=truncation_info['total'] if truncation_info else len(items),
            truncated=(truncation_info is not None),
            truncation_info=truncation_info
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
