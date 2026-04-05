"""Shared utilities for file operations."""

import time
from functools import lru_cache
from pathlib import Path
from typing import Optional

_GITIGNORE_SPEC_REGISTRY = {}

# Performance metrics for gitignore filtering
_gitignore_filter_times = []
_gitignore_spec_hits = 0


def _register_gitignore_spec(gitignore_spec) -> int:
    """Register a PathSpec for cached lookups and return its key.

    Args:
        gitignore_spec: PathSpec object to register

    Returns:
        Registry key for the PathSpec object
    """
    if gitignore_spec is None:
        return 0
    key = id(gitignore_spec)
    _GITIGNORE_SPEC_REGISTRY[key] = gitignore_spec
    return key


@lru_cache(maxsize=1000)
def _is_ignored_cached(path_str: str, repo_root_str: str, spec_key: int) -> bool:
    """Cached version of gitignore check.

    Args:
        path_str: String representation of path to check
        repo_root_str: String representation of repository root
        spec_key: Registry key for the PathSpec object

    Returns:
        True if path is ignored by gitignore spec
    """
    gitignore_spec = _GITIGNORE_SPEC_REGISTRY.get(spec_key)
    if gitignore_spec is None:
        return False

    from utils.gitignore_filter import is_path_ignored

    path = Path(path_str)
    repo_root = Path(repo_root_str)
    is_ignored, _ = is_path_ignored(path, repo_root, gitignore_spec)
    return is_ignored


def _is_reserved_windows_name(name: str) -> bool:
    """Check if filename is a reserved Windows device name.

    Args:
        name: Filename to check (without path)

    Returns:
        True if name is reserved (e.g., CON, PRN, NUL)
    """
    if not name:
        return False
    base = name.upper().split('.')[0]
    return base in {
        'CON', 'PRN', 'AUX', 'NUL',
        'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
        'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9'
    }


class GitignoreFilter:
    """Centralized gitignore filtering logic.

    This class provides a single interface for checking if paths should be
    excluded based on .gitignore rules, combining fast-path checks with
    full gitignore spec evaluation.

    Example:
        filter = GitignoreFilter(repo_root=Path("/project"), gitignore_spec=spec)
        if filter.is_ignored(path):
            continue  # Skip this path
    """

    def __init__(
        self,
        repo_root: Path,
        gitignore_spec = None
    ):
        """Initialize the gitignore filter.

        Args:
            repo_root: Repository root directory
            gitignore_spec: Optional PathSpec for .gitignore filtering
        """
        self.repo_root = repo_root
        self.gitignore_spec = gitignore_spec
        self._spec_key = _register_gitignore_spec(gitignore_spec) if gitignore_spec else None

    def is_ignored(self, path: Path) -> bool:
        """Check if a path should be ignored by gitignore rules.

        Args:
            path: Path object to check

        Returns:
            True if path should be ignored, False otherwise
        """
        global _gitignore_spec_hits
        start_time = time.time()

        # Full gitignore check (only if spec is provided)
        if self.gitignore_spec is not None and self._spec_key is not None:
            # Only check paths within the repo
            try:
                path.relative_to(self.repo_root)
                is_ignored = _is_ignored_cached(str(path), str(self.repo_root), self._spec_key)
                if is_ignored:
                    _gitignore_spec_hits += 1
                _gitignore_filter_times.append(time.time() - start_time)
                return is_ignored
            except ValueError:
                # Path is outside repo, don't filter
                pass

        _gitignore_filter_times.append(time.time() - start_time)
        return False

    def should_include(self, path: Path) -> bool:
        """Check if a path should be included (inverse of is_ignored).

        This is provided for readability when filtering:
            files = [f for f in files if filter.should_include(f)]

        Args:
            path: Path object to check

        Returns:
            True if path should be included, False if ignored
        """
        return not self.is_ignored(path)


def get_gitignore_filter_metrics() -> dict:
    """Get performance metrics for gitignore filtering operations.

    Returns:
        Dictionary with metrics:
        - total_checks: Total number of filter checks
        - spec_hits: Number of matches by gitignore spec
        - avg_filter_time: Average filter time in seconds
        - cache_hit_rate: Estimated LRU cache hit rate
    """
    if not _gitignore_filter_times:
        return {
            "total_checks": 0,
            "spec_hits": 0,
            "avg_filter_time": 0,
            "cache_hit_rate": 0
        }

    total_checks = len(_gitignore_filter_times)
    total_hits = _gitignore_spec_hits

    # Estimate cache hit rate from _is_ignored_cached
    try:
        from functools import _is_ignored_cached as cached_func
        cache_info = cached_func.cache_info() if hasattr(cached_func, 'cache_info') else None
        cache_hit_rate = cache_info.hits / (cache_info.hits + cache_info.misses) if cache_info and (cache_info.hits + cache_info.misses) > 0 else 0
    except:
        cache_hit_rate = 0

    return {
        "total_checks": total_checks,
        "spec_hits": _gitignore_spec_hits,
        "avg_filter_time": sum(_gitignore_filter_times) / total_checks,
        "cache_hit_rate": cache_hit_rate
    }


def clear_gitignore_filter_metrics():
    """Clear all accumulated metrics for testing or monitoring reset."""
    global _gitignore_spec_hits
    _gitignore_filter_times.clear()
    _gitignore_spec_hits = 0
    # Clear LRU cache for _is_ignored_cached
    _is_ignored_cached.cache_clear()
