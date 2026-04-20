"""Tool execution utilities.

This package provides command execution, file editing, and result formatting
capabilities for the bone-agent AI assistant.
"""

import logging

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

# search_plugins — core meta-tool for plugin discovery
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
# Backward compatibility: Re-export helpers at package level
# This allows imports like: from tools.base import tool
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
    from .helpers.loader import load_plugin_tools
    load_plugin_tools()
except Exception as e:
    _logger.debug("Failed to load plugin tools: %s", e)

# Make base module available for backward compatibility
import sys
from types import ModuleType

# Create a synthetic 'base' module that re-exports from helpers
_base_module = ModuleType('tools.base')
_base_module.__dict__.update({
    'ToolDefinition': ToolDefinition,
    'ToolRegistry': ToolRegistry,
    'tool': tool,
    'build_context': build_context,
    'get_tool_schemas': get_tool_schemas,
    'TOOLS': TOOLS,
})
sys.modules['tools.base'] = _base_module

# Create synthetic modules for other helpers
_formatters_module = ModuleType('tools.formatters')
_formatters_module.__dict__.update({
    'format_tool_result': format_tool_result,
    'format_file_result': format_file_result,
    '_build_diff': _build_diff,
    '_detect_newline': _detect_newline,
})
sys.modules['tools.formatters'] = _formatters_module

_file_helpers_module = ModuleType('tools.file_helpers')
from .helpers.file_helpers import (
    _is_reserved_windows_name,
    GitignoreFilter,
)
_file_helpers_module.__dict__.update({
    '_is_reserved_windows_name': _is_reserved_windows_name,
    'GitignoreFilter': GitignoreFilter,
})
sys.modules['tools.file_helpers'] = _file_helpers_module

# Path resolver module
_path_resolver_module = ModuleType('tools.path_resolver')
from .helpers.path_resolver import PathResolver
_path_resolver_module.__dict__.update({
    'PathResolver': PathResolver,
})
sys.modules['tools.path_resolver'] = _path_resolver_module

_converters_module = ModuleType('tools.converters')
from .helpers.converters import coerce_int, coerce_bool
_converters_module.__dict__.update({
    'coerce_int': coerce_int,
    'coerce_bool': coerce_bool,
})
sys.modules['tools.converters'] = _converters_module

_loader_module = ModuleType('tools.loader')
from .helpers.loader import (
    discover_tools,
    load_builtin_tools,
    load_plugin_tools,
    load_all_tools,
    list_registered_tools,
)
_loader_module.__dict__.update({
    'discover_tools': discover_tools,
    'load_builtin_tools': load_builtin_tools,
    'load_plugin_tools': load_plugin_tools,
    'load_all_tools': load_all_tools,
    'list_registered_tools': list_registered_tools,
})
sys.modules['tools.loader'] = _loader_module

_parallel_executor_module = ModuleType('tools.parallel_executor')
from .helpers.parallel_executor import ToolCall, ToolResult, ParallelToolExecutor
_parallel_executor_module.__dict__.update({
    'ToolCall': ToolCall,
    'ToolResult': ToolResult,
    'ParallelToolExecutor': ParallelToolExecutor,
})
sys.modules['tools.parallel_executor'] = _parallel_executor_module

