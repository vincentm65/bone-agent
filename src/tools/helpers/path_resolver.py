"""Path resolution and validation utilities.

This module provides centralized path validation and resolution logic,
ensuring consistent behavior across all tools that work with file paths.
"""

import os
import time
from pathlib import Path
from typing import Optional, Tuple

from .file_helpers import _is_reserved_windows_name

# Performance metrics tracking
_path_resolution_times = []
_path_validation_errors = {}


class PathResolver:
    """Centralized path resolution and validation.

    This class provides a single source of truth for path validation logic,
    including Windows-specific checks, path resolution, and gitignore filtering.

    Example:
        resolver = PathResolver(repo_root=Path("/project"), gitignore_spec=spec)
        resolved_path, error = resolver.resolve_and_validate("src/file.py")
        if error:
            return f"Error: {error}"
        # Use resolved_path...
    """

    def __init__(
        self,
        repo_root: Path,
        gitignore_spec = None,
        vault_path: Path = None
    ):
        """Initialize the path resolver.

        Args:
            repo_root: Repository root directory for resolving relative paths
            gitignore_spec: Optional PathSpec for .gitignore filtering
            vault_path: Optional Obsidian vault root (allowed as second base path)
        """
        self.repo_root = repo_root
        self.vault_path = vault_path
        self.gitignore_spec = gitignore_spec

    def resolve_and_validate(
        self,
        path_str: str,
        check_gitignore: bool = True,
        must_exist: bool = True,
        must_be_file: bool = False,
        must_be_dir: bool = False,
        enforce_boundary: bool = False,
    ) -> Tuple[Optional[Path], Optional[str]]:
        """Validate and resolve a path string.

        Performs comprehensive validation including:
        - Windows filename validation (invalid chars, reserved names)
        - Path resolution (absolute vs relative)
        - Gitignore filtering (optional, within repo only)
        - Path existence check (optional)
        - Type validation (file/directory, optional)

        Args:
            path_str: Path string to validate
            check_gitignore: Whether to apply gitignore filtering
            must_exist: Whether the path must exist on disk
            must_be_file: Whether the path must be a file (requires must_exist=True)
            must_be_dir: Whether the path must be a directory (requires must_exist=True)
            enforce_boundary: Whether to restrict paths to repo_root or vault_path (default: False)

        Returns:
            Tuple of (resolved_path, error_message)
            - resolved_path: Path object if valid, None if invalid
            - error_message: None if valid, error description if invalid
        """
        start_time = time.time()

        try:
            # Step 1: Validate filename for Windows-specific issues
            if os.name == 'nt':  # Windows-specific validation
                # Check for invalid characters
                invalid_chars = '<>:"|?*[]{}"\n\r\t'
                if any(char in path_str for char in invalid_chars):
                    elapsed = time.time() - start_time
                    _track_validation_error("invalid_chars")
                    _path_resolution_times.append(elapsed)
                    return None, f"Filename contains invalid characters: {invalid_chars}"

                # Check for reserved device names
                filename = Path(path_str).name
                if _is_reserved_windows_name(filename):
                    elapsed = time.time() - start_time
                    _track_validation_error("reserved_name")
                    _path_resolution_times.append(elapsed)
                    return None, f"Filename is a reserved Windows device name: {filename}"

            # Step 2: Resolve the path
            path = Path(path_str)
            if not path.is_absolute():
                path = self.repo_root / path

            # Resolve to absolute path (handles .. and symlinks)
            path = path.resolve()

            # Step 2b: Security boundary — path must be within repo_root or vault_path
            if enforce_boundary:
                try:
                    path.relative_to(self.repo_root)
                except ValueError:
                    if self.vault_path is not None:
                        try:
                            path.relative_to(self.vault_path)
                        except ValueError:
                            elapsed = time.time() - start_time
                            _track_validation_error("outside_allowed_roots")
                            _path_resolution_times.append(elapsed)
                            return None, f"Path is outside allowed directories: {path_str}"
                    else:
                        elapsed = time.time() - start_time
                        _track_validation_error("outside_repo")
                        _path_resolution_times.append(elapsed)
                        return None, f"Path is outside repository: {path_str}"

            # Step 3: Check existence if required
            if must_exist:
                if not path.exists():
                    elapsed = time.time() - start_time
                    _track_validation_error("not_found")
                    _path_resolution_times.append(elapsed)
                    return None, f"Path not found: {path_str}"

                # Step 4: Validate type if required
                if must_be_file and not path.is_file():
                    elapsed = time.time() - start_time
                    _track_validation_error("not_a_file")
                    _path_resolution_times.append(elapsed)
                    return None, f"Path is not a file: {path_str}"

                if must_be_dir and not path.is_dir():
                    elapsed = time.time() - start_time
                    _track_validation_error("not_a_dir")
                    _path_resolution_times.append(elapsed)
                    return None, f"Path is not a directory: {path_str}"

            # Step 5: Check gitignore if requested and within repo
            if check_gitignore and self.gitignore_spec is not None:
                # Only check gitignore for paths within the repo
                try:
                    path.relative_to(self.repo_root)
                    from .file_helpers import _is_ignored_cached, _register_gitignore_spec

                    # Full gitignore check
                    spec_key = _register_gitignore_spec(self.gitignore_spec)
                    if _is_ignored_cached(str(path), str(self.repo_root), spec_key):
                        elapsed = time.time() - start_time
                        _track_validation_error("gitignore_filtered")
                        _path_resolution_times.append(elapsed)
                        return None, f"Path is excluded by .gitignore: {path_str}"
                except ValueError:
                    # Path is outside repo, skip gitignore check
                    pass

            # Success - track timing
            elapsed = time.time() - start_time
            _path_resolution_times.append(elapsed)
            return path, None

        except OSError as e:
            elapsed = time.time() - start_time
            _track_validation_error("os_error")
            _path_resolution_times.append(elapsed)
            return None, f"Error accessing path '{path_str}': {e}"
        except Exception as e:
            elapsed = time.time() - start_time
            _track_validation_error("unexpected_error")
            _path_resolution_times.append(elapsed)
            return None, f"Unexpected error resolving path '{path_str}': {e}"


def _track_validation_error(error_type: str):
    """Track validation errors for metrics.

    Args:
        error_type: Type of validation error
    """
    _path_validation_errors[error_type] = _path_validation_errors.get(error_type, 0) + 1


def get_path_resolver_metrics() -> dict:
    """Get performance metrics for path resolution operations.

    Returns:
        Dictionary with metrics:
        - total_resolutions: Total number of path resolutions
        - avg_resolution_time: Average resolution time in seconds
        - max_resolution_time: Maximum resolution time
        - min_resolution_time: Minimum resolution time
        - validation_errors: Dict of error types and counts
    """
    if not _path_resolution_times:
        return {
            "total_resolutions": 0,
            "avg_resolution_time": 0,
            "max_resolution_time": 0,
            "min_resolution_time": 0,
            "validation_errors": _path_validation_errors.copy()
        }

    return {
        "total_resolutions": len(_path_resolution_times),
        "avg_resolution_time": sum(_path_resolution_times) / len(_path_resolution_times),
        "max_resolution_time": max(_path_resolution_times),
        "min_resolution_time": min(_path_resolution_times),
        "validation_errors": _path_validation_errors.copy()
    }


def clear_path_resolver_metrics():
    """Clear all accumulated metrics for testing or monitoring reset."""
    _path_resolution_times.clear()
    _path_validation_errors.clear()
