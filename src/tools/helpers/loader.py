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

        # For user tools (not in src/utils/tools/), set package to None
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


def load_builtin_tools() -> int:
    """Load built-in tools from src/utils/tools/.

    Returns:
        Number of tools loaded

    Note:
        Built-in tools are already imported in __init__.py, which triggers
        @tool decorator registration. This function returns the count of
        currently registered built-in tools.
    """
    # Built-in tools are already imported in utils/tools/__init__.py
    # which triggers @tool decorator registration.
    # Just return the count of tools currently in registry.
    return ToolRegistry.tool_count()


def load_plugin_tools() -> int:
    """Load plugin tools from tool_plugins/ directory.

    Plugin modules are imported, which triggers @tool(tier="plugin") decorator
    registration into the PluginManifest (NOT ToolRegistry). This keeps plugin
    schemas out of the LLM context window until activated via search_plugins.

    Returns:
        Number of plugin modules loaded

    Note:
        - tool_plugins/ directory at repository root
        - Plugin tools registered in PluginManifest, not ToolRegistry
    """
    from .plugin_manifest import plugin_manifest

    # Get repository root (assumes we're in src/tools/helpers/)
    current_dir = Path(__file__).parent
    repo_root = current_dir.parent.parent.parent

    # Define plugin tool directories
    plugin_directories = [
        str(repo_root / "tool_plugins"),
    ]

    # Discover tools in plugin directories
    modules_loaded = discover_tools(plugin_directories)

    logger.info(
        f"Plugin manifest: {plugin_manifest.plugin_count()} plugins available "
        f"(categories: {plugin_manifest.get_categories()})"
    )

    return modules_loaded


def load_all_tools() -> int:
    """Load all tools (built-in and plugin tools).

    Returns:
        Total number of tools loaded

    Discovery order:
        1. Built-in tools (src/tools/*.py)
        2. Plugin tools (tool_plugins/*.py)

    Note:
        Plugin tools loaded later can override built-in tools.
    """
    logger.info("Starting tool loading...")

    # Load built-in tools first
    builtin_count = load_builtin_tools()

    # Then load plugin tools (can override built-ins)
    plugin_count = load_plugin_tools()

    total_count = builtin_count + plugin_count
    total_registered = ToolRegistry.tool_count()

    logger.info(
        f"Tool loading complete: {builtin_count} built-in + "
        f"{plugin_count} plugin modules = {total_count} modules, "
        f"{total_registered} tools registered"
    )

    return total_count


def list_registered_tools() -> List[str]:
    """List names of all registered tools.

    Returns:
        List of tool names
    """
    return [tool.name for tool in ToolRegistry.get_all()]


def list_tools_for_mode(mode: str) -> List[str]:
    """List names of tools allowed in a specific mode.

    Args:
        mode: Interaction mode ('edit' or 'plan')

    Returns:
        List of tool names
    """
    return [tool.name for tool in ToolRegistry.get_tools_for_mode(mode)]
