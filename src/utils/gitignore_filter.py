"""Centralized .gitignore filtering using pathspec library."""

import logging
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Always allow .gitignore itself to be read/edited
ALWAYS_ALLOWED_FILES = {".gitignore"}

# Try to import pathspec at module level
try:
    import pathspec
except ImportError:
    pathspec = None
    logger.warning("pathspec library not installed - .gitignore filtering disabled")


def load_gitignore_spec(repo_root: Path):
    """Load .gitignore patterns into a PathSpec object.

    Args:
        repo_root: Repository root directory

    Returns:
        pathspec.PathSpec or None if .gitignore doesn't exist
    """
    # Return None early if pathspec is not available
    if pathspec is None:
        return None

    gitignore_path = repo_root / ".gitignore"

    if not gitignore_path.exists():
        return None

    try:
        # Read .gitignore patterns
        patterns = gitignore_path.read_text(encoding="utf-8").splitlines()

        # Create PathSpec with gitwildmatch (git's pattern matching)
        spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)
        return spec

    except Exception as e:
        logger.warning(f"Failed to load .gitignore: {e}")
        return None


def is_path_ignored(
    path: Path, repo_root: Path, gitignore_spec
) -> Tuple[bool, Optional[str]]:
    """Check if a path is ignored by .gitignore.

    Args:
        path: Absolute path to check
        repo_root: Repository root directory
        gitignore_spec: pathspec.PathSpec object (or None)

    Returns:
        Tuple of (is_ignored, matched_pattern)
        - is_ignored: True if path should be blocked
        - matched_pattern: The pattern that matched (or None)
    """
    # No filtering if no .gitignore
    if gitignore_spec is None:
        return False, None

    # Always allow .gitignore itself
    if path.name in ALWAYS_ALLOWED_FILES:
        return False, None

    # Get relative path from repo root (gitignore only applies within repo)
    try:
        rel_path = path.relative_to(repo_root)
    except ValueError:
        # Path is outside repo - gitignore doesn't apply
        return False, None

    # Convert to forward slashes (git convention)
    rel_path_str = str(rel_path).replace("\\", "/")

    # Check if path matches any .gitignore pattern
    # pathspec.match_file() returns True if the file should be ignored
    if gitignore_spec.match_file(rel_path_str):
        # Find which pattern matched (for better error messages)
        matched_pattern = _find_matching_pattern(rel_path_str, gitignore_spec)
        return True, matched_pattern

    return False, None


def _find_matching_pattern(path_str: str, gitignore_spec) -> Optional[str]:
    """Find which .gitignore pattern matched a path.

    This is for better error messages.

    Args:
        path_str: Relative path string (with forward slashes)
        gitignore_spec: pathspec.PathSpec object

    Returns:
        The matching pattern string, or None
    """
    try:
        # PathSpec stores patterns internally
        for pattern in gitignore_spec.patterns:
            if pattern.match_file(path_str):
                # Return the original pattern string
                return pattern.pattern
    except Exception:
        pass

    return None


