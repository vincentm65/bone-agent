"""Tool execution utilities.

This package provides command execution, file editing, and result formatting
capabilities for the vmCode AI assistant.
"""

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
from . import task_list
from . import select_option

# Tool schema exports (now in helpers/base.py, merged from definitions.py)
from .helpers.base import TOOLS, _tools_for_mode

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
    # Tool definitions
    'TOOLS',
    '_tools_for_mode',
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
    get_tools_for_mode,
)

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
    'get_tools_for_mode': get_tools_for_mode,
    'TOOLS': TOOLS,
    '_tools_for_mode': _tools_for_mode,
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
    _is_fast_ignored,
    _is_ignored_cached,
    _register_gitignore_spec,
    _is_reserved_windows_name,
)
_file_helpers_module.__dict__.update({
    '_is_fast_ignored': _is_fast_ignored,
    '_is_ignored_cached': _is_ignored_cached,
    '_register_gitignore_spec': _register_gitignore_spec,
    '_is_reserved_windows_name': _is_reserved_windows_name,
})
sys.modules['tools.file_helpers'] = _file_helpers_module

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
    load_user_tools,
    load_all_tools,
    list_registered_tools,
    list_tools_for_mode,
)
_loader_module.__dict__.update({
    'discover_tools': discover_tools,
    'load_builtin_tools': load_builtin_tools,
    'load_user_tools': load_user_tools,
    'load_all_tools': load_all_tools,
    'list_registered_tools': list_registered_tools,
    'list_tools_for_mode': list_tools_for_mode,
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

