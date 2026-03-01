"""Example user tool demonstrating the new decorator-based tool API.

This is a sample custom tool that users can create in the user_tools/ directory.
Simply drop a .py file here with a @tool decorated function, and it will
be automatically discovered and loaded when vmCode starts.
"""

import sys
from pathlib import Path

# Add src to path so we can import the tool decorator
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tools.base import tool


@tool(
    name="count_lines_in_files",
    description="Count total lines in Python files in a directory.",
    parameters={
        "type": "object",
        "properties": {
            "directory": {
                "type": "string",
                "description": "Directory to scan (relative or absolute path)"
            },
            "recursive": {
                "type": "boolean",
                "description": "Scan recursively (default: false)"
            }
        },
        "required": ["directory"]
    },
    allowed_modes=["learn", "plan", "edit"],
    requires_approval=False
)
def count_lines_in_files(
    directory: str,
    repo_root: Path,
    recursive: bool = False,
    gitignore_spec = None
) -> str:
    """Count lines in Python files in a directory.

    Args:
        directory: Directory path to scan
        repo_root: Repository root (injected by context)
        recursive: Whether to scan recursively
        gitignore_spec: Gitignore spec for filtering (injected by context)

    Returns:
        Formatted result with exit_code and line count summary
    """
    from tools.file_helpers import _is_fast_ignored, _is_ignored_cached, _register_gitignore_spec

    # Resolve path
    path = Path(directory)
    if not path.is_absolute():
        path = repo_root / path

    # Validate path exists
    if not path.exists():
        return f"exit_code=1\nError: Directory not found: {directory}"

    if not path.is_dir():
        return f"exit_code=1\nError: Path is not a directory: {directory}"

    # Collect files
    if recursive:
        files = path.rglob("*.py")
    else:
        files = path.glob("*.py")

    # Count lines
    total_lines = 0
    file_count = 0
    spec_key = _register_gitignore_spec(gitignore_spec) if gitignore_spec else None

    for file_path in files:
        # Skip if gitignored
        if gitignore_spec:
            if _is_fast_ignored(file_path):
                continue
            if _is_ignored_cached(str(file_path), str(repo_root), spec_key):
                continue

        try:
            lines = len(file_path.read_text(encoding="utf-8", errors="replace").splitlines())
            total_lines += lines
            file_count += 1
        except (OSError, UnicodeDecodeError):
            # Skip files we can't read
            continue

    return f"exit_code=0\nFound {file_count} Python files with {total_lines} total lines in {directory}"


@tool(
    name="find_empty_files",
    description="Find all empty files in a directory.",
    parameters={
        "type": "object",
        "properties": {
            "directory": {
                "type": "string",
                "description": "Directory to scan (relative or absolute path)"
            },
            "recursive": {
                "type": "boolean",
                "description": "Scan recursively (default: false)"
            }
        },
        "required": ["directory"]
    },
    allowed_modes=["learn", "plan", "edit"],
    requires_approval=False
)
def find_empty_files(
    directory: str,
    repo_root: Path,
    recursive: bool = False,
    gitignore_spec = None
) -> str:
    """Find empty files in a directory.

    Args:
        directory: Directory path to scan
        repo_root: Repository root (injected by context)
        recursive: Whether to scan recursively
        gitignore_spec: Gitignore spec for filtering (injected by context)

    Returns:
        Formatted result with exit_code and list of empty files
    """
    from tools.file_helpers import _is_fast_ignored, _is_ignored_cached, _register_gitignore_spec

    # Resolve path
    path = Path(directory)
    if not path.is_absolute():
        path = repo_root / path

    # Validate path exists
    if not path.exists():
        return f"exit_code=1\nError: Directory not found: {directory}"

    if not path.is_dir():
        return f"exit_code=1\nError: Path is not a directory: {directory}"

    # Collect files
    if recursive:
        files = path.rglob("*")
    else:
        files = path.glob("*")

    # Find empty files
    empty_files = []
    spec_key = _register_gitignore_spec(gitignore_spec) if gitignore_spec else None

    for file_path in files:
        if not file_path.is_file():
            continue

        # Skip if gitignored
        if gitignore_spec:
            if _is_fast_ignored(file_path):
                continue
            if _is_ignored_cached(str(file_path), str(repo_root), spec_key):
                continue

        # Check if file is empty
        if file_path.stat().st_size == 0:
            try:
                rel_path = file_path.relative_to(repo_root)
                empty_files.append(str(rel_path))
            except ValueError:
                empty_files.append(str(file_path))

    if not empty_files:
        return f"exit_code=0\nNo empty files found in {directory}"

    result_lines = [f"exit_code=0\nFound {len(empty_files)} empty files in {directory}:"]
    result_lines.extend(f"  - {f}" for f in empty_files)

    return "\n".join(result_lines)
