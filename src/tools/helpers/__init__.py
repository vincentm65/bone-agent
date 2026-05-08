"""Tool infrastructure and helper utilities.

This subpackage provides the core infrastructure for tool registration,
execution, and supporting utilities. It is not intended to be imported
directly by end users - import from tools/ instead for backward compatibility.

For creating custom tools, use:
    from tools import tool  # or from tools.helpers.base import tool
"""

# Core infrastructure
from .base import (
    ToolDefinition,
    ToolRegistry,
    tool,
    build_context,
    get_tool_schemas,
    get_terminal_policy,
    TERMINAL_NONE,
    TERMINAL_YIELD,
    TERMINAL_STOP,
    TOOLS,
)

# File operation helpers
from .file_helpers import (
    _is_reserved_windows_name,
    GitignoreFilter,
)

# Path resolution helpers
from .path_resolver import PathResolver

# Result formatting helpers
from .formatters import (
    format_tool_result,
    format_file_result,
    _build_diff,
    _detect_newline,
)

# Type conversion helpers
from .converters import (
    coerce_int,
    coerce_bool,
)

# Tool loading and discovery
from .loader import (
    discover_tools,
)

# Plugin manifest for on-demand tool discovery
from .plugin_manifest import PluginManifest, plugin_manifest

# Parallel execution
from .parallel_executor import (
    ToolCall,
    ToolResult,
    ParallelToolExecutor,
)

__all__ = [
    # Core infrastructure
    'ToolDefinition',
    'ToolRegistry',
    'tool',
    'build_context',
    'get_tool_schemas',
    'get_terminal_policy',
    'TERMINAL_NONE',
    'TERMINAL_YIELD',
    'TERMINAL_STOP',
    'TOOLS',
    # File operation helpers
    '_is_reserved_windows_name',
    'GitignoreFilter',
    # Path resolution helpers
    'PathResolver',
    # Result formatting helpers
    'format_tool_result',
    'format_file_result',
    '_build_diff',
    '_detect_newline',
    # Type conversion helpers
    'coerce_int',
    'coerce_bool',
    # Tool loading and discovery
    'discover_tools',
    # Plugin manifest
    'PluginManifest',
    'plugin_manifest',
    # Parallel execution
    'ToolCall',
    'ToolResult',
    'ParallelToolExecutor',
]
