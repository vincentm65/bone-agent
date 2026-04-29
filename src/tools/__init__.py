"""Tool execution utilities.

This package provides command execution, file editing, and result formatting
capabilities for the bone-agent AI assistant.
"""

import logging
import sys
from pathlib import Path

_logger = logging.getLogger(__name__)

# Command execution (now in shell.py)
from .shell import (
    confirm_tool,
    run_shell_command,
)

# UI components
from ui.tool_confirmation import ToolConfirmationPanel

# File editing (now in edit.py)
from .edit import (
    _resolve_repo_path,
    preview_edit_file,
    run_edit_file,
)

# Result formatting (now in helpers/)
from .helpers.formatters import (
    format_tool_result,
    format_file_result,
    _build_diff,
    _detect_newline,
)

# File operations
from .directory import list_directory
from .create_file import create_file
from .file_reader import read_file

# Constants
from . import constants

# Tool definitions
# Import tool modules to trigger @tool decorator registration
# These modules register themselves when imported
from . import file_reader
from . import directory
from . import create_file
from . import edit  # edit.py now contains both core logic and @tool decorators
from . import rg_search
from . import shell  # shell.py now contains both core logic and @tool decorators
from . import web_search
from . import sub_agent
# review_sub_agent is not an LLM tool — used as a /review slash command in ui.commands

from . import task_list
from . import select_option

# search_plugins — core meta-tool for capability discovery and loading
from . import search_plugins

# Obsidian tools — conditional registration (register() pattern, NOT @tool at import)
# Only imported and registered when vault is configured and enabled.
# This ensures zero token cost when no vault is linked.
try:
    from utils.settings import obsidian_settings
    if obsidian_settings.is_active():
        from . import obsidian as _obsidian_mod
        _obsidian_mod.register()
except Exception as e:
    _logger.debug("Obsidian tools not loaded: %s", e)

# Tool schema exports (now in helpers/base.py, merged from definitions.py)
from .helpers.base import TOOLS

__all__ = [
    # Command execution
    'confirm_tool',
    'run_shell_command',
    # UI components
    'ToolConfirmationPanel',
    # File editing
    '_resolve_repo_path',
    'preview_edit_file',
    'run_edit_file',
    # Formatters
    'format_tool_result',
    'format_file_result',
    '_build_diff',
    '_detect_newline',
    # File operations
    'read_file',
    'list_directory',
    'create_file',
    # Constants
    'constants',
    # Tool definitions
    'TOOLS',
]

# =============================================================================
# Re-export helpers at package level
# =============================================================================
from .helpers import (
    ToolDefinition,
    ToolRegistry,
    tool,
    build_context,
    get_tool_schemas,
    get_terminal_policy,
    TERMINAL_NONE,
    TERMINAL_YIELD,
    TERMINAL_STOP,
)

# Apply disabled tools from settings (after all tools are registered)
try:
    from utils.settings import tool_settings
    for tool_name in tool_settings.disabled_tools:
        ToolRegistry.disable(tool_name)
except Exception as e:
    _logger.debug("Failed to apply disabled tools: %s", e)

# Load plugin tools into the PluginManifest (not ToolRegistry).
# Plugin modules with @tool(tier="plugin") register into the manifest
# and are only activated in ToolRegistry on-demand via search_plugins.
try:
    from .helpers.loader import discover_tools, ensure_user_tool_plugins_dir
    from .helpers.plugin_manifest import plugin_manifest

    src_dir = str(Path(__file__).resolve().parents[1])
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    discover_tools([str(ensure_user_tool_plugins_dir())])

    _logger.info(
        "Plugin manifest: %s plugins available (categories: %s)",
        plugin_manifest.plugin_count(),
        plugin_manifest.get_categories(),
    )

    # Re-apply disabled_tools now that plugins are in the manifest
    for tool_name in tool_settings.disabled_tools:
        if plugin_manifest.has_plugin(tool_name):
            ToolRegistry.disable(tool_name)
except Exception as e:
    _logger.debug("Failed to load plugin tools: %s", e)
