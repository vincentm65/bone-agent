"""Tool infrastructure and helper utilities.

This subpackage provides the core infrastructure for tool registration,
execution, and supporting utilities. It is not intended to be imported
directly by end users - import from tools/ instead for backward compatibility.

For creating custom tools, use:
    from tools import tool  # or from tools.base import tool
"""

# Core infrastructure
from .base import (
    ToolDefinition,
    ToolRegistry,
    tool,
    build_context,
    get_tool_schemas,
    get_tools_for_mode,
    TOOLS,
    _tools_for_mode,
)

# File operation helpers
from .file_helpers import (
    _is_fast_ignored,
    _is_ignored_cached,
    _register_gitignore_spec,
    _is_reserved_windows_name,
)

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
    load_builtin_tools,
    load_user_tools,
    load_all_tools,
    list_registered_tools,
    list_tools_for_mode,
)

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
    'get_tools_for_mode',
    'TOOLS',
    '_tools_for_mode',
    # File operation helpers
    '_is_fast_ignored',
    '_is_ignored_cached',
    '_register_gitignore_spec',
    '_is_reserved_windows_name',
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
    'load_builtin_tools',
    'load_user_tools',
    'load_all_tools',
    'list_registered_tools',
    'list_tools_for_mode',
    # Parallel execution
    'ToolCall',
    'ToolResult',
    'ParallelToolExecutor',
]
