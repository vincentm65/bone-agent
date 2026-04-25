"""Tool auto-discovery and loading mechanism.

This module provides automatic discovery and loading of tools from
multiple directories. Tools are imported to trigger @tool decorator
registration.
"""

import importlib.util
import logging
import sys
from pathlib import Path
from typing import List, Optional

from .base import ToolRegistry

logger = logging.getLogger(__name__)


def _is_python_file(path: Path) -> bool:
    """Check if a file is a Python module.

    Args:
        path: File path to check

    Returns:
        True if file is a .py file (not __pycache__ or test file)
    """
    return (
        path.suffix == ".py"
        and path.name != "__init__.py"
        and not path.name.startswith("test_")
        and not path.name.startswith("_")
    )


def _load_module_from_path(module_name: str, file_path: Path) -> Optional[object]:
    """Load a Python module from a file path.

    Args:
        module_name: Name to give the module
        file_path: Path to the Python file

    Returns:
        Loaded module or None if loading failed
    """
    try:
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            logger.warning(f"Could not load spec for {file_path}")
            return None

        module = importlib.util.module_from_spec(spec)

        # For external tool modules (not in src/tools/), set package to None
        # to force absolute imports instead of relative imports
        from tools import __file__ as tools_init_file
        tools_dir = Path(tools_init_file).parent

        # User tools are those not in the main tools directory
        # (helper modules are in src/tools/helpers/)
        if file_path.parent != tools_dir and file_path.parent != tools_dir / "helpers":
            # User tool - set to None to force absolute imports
            module.__package__ = None

        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        logger.debug(f"Successfully loaded module: {module_name}")
        return module

    except Exception as e:
        logger.warning(f"Failed to load module {module_name} from {file_path}: {e}")
        return None


def discover_tools(directories: List[str]) -> int:
    """Discover and load tools from specified directories.

    This scans directories for Python files and imports them,
    which triggers @tool decorator registration.

    Args:
        directories: List of directory paths to scan

    Returns:
        Number of tools successfully loaded

    Note:
        - Only .py files are considered (excluding __pycache__, tests)
        - Import errors are logged but don't stop discovery
        - User tools can override built-in tools (with warning)
    """
    initial_count = ToolRegistry.tool_count()
    loaded_count = 0

    for directory in directories:
        dir_path = Path(directory)

        if not dir_path.exists():
            logger.debug(f"Tool directory does not exist: {directory}")
            continue

        if not dir_path.is_dir():
            logger.warning(f"Tool path is not a directory: {directory}")
            continue

        logger.info(f"Discovering tools in: {directory}")

        # Find all Python files
        python_files = [f for f in dir_path.iterdir() if _is_python_file(f)]

        for py_file in python_files:
            # Create unique module name
            module_name = f"tools_{py_file.stem}_{hash(str(py_file)) & 0xFFFFFFFF}"

            # Skip modules already loaded (e.g. cron re-calling discover_tools)
            if module_name in sys.modules:
                logger.debug(f"Module already loaded, skipping: {module_name}")
                continue

            module = _load_module_from_path(module_name, py_file)
            if module:
                loaded_count += 1

    final_count = ToolRegistry.tool_count()
    new_tools = final_count - initial_count

    logger.info(
        f"Tool discovery complete: Loaded {loaded_count} modules, "
        f"registered {new_tools} new tools (total: {final_count})"
    )

    return new_tools


def list_registered_tools() -> List[str]:
    """List names of all registered tools.

    Returns:
        List of tool names
    """
    return [tool.name for tool in ToolRegistry.get_all()]



