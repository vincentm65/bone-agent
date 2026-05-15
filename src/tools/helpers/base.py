"""Tool registry and decorator for automatic tool registration.

This module provides the core infrastructure for defining tools with a
decorator-based pattern. Tools are automatically registered and can be
filtered by interaction mode.
"""

import logging
import inspect
from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path


_logger = logging.getLogger(__name__)


# Terminal policy constants for thinking indicator handoff
TERMINAL_NONE = "none"      # Indicator keeps running (non-interactive tools)
TERMINAL_YIELD = "yield"    # Indicator pauses, clears line, tool takes over terminal (Live/prompt_toolkit)
TERMINAL_STOP = "stop"      # Indicator fully stops (approval prompts need clean terminal)


@dataclass
class ToolDefinition:
    """Definition of a tool including metadata and execution handler.

    Attributes:
        name: Tool identifier (e.g., "read_file")
        description: Human-readable description of what the tool does
        parameters: JSON Schema for tool parameters
        requires_approval: Whether this tool requires user confirmation
        terminal_policy: How this tool interacts with the thinking indicator
        handler: Function that executes the tool
    """
    name: str
    description: str
    parameters: Dict[str, Any]
    requires_approval: bool = False
    terminal_policy: str = TERMINAL_NONE  # Default: indicator keeps running
    handler: Optional[Callable] = None

    def to_openai_schema(self) -> Dict[str, Any]:
        """Convert tool definition to OpenAI function-calling schema.

        Returns:
            Dictionary in OpenAI function format
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters
            }
        }

    def execute(self, arguments: Dict[str, Any], context: Dict[str, Any]) -> str:
        """Execute the tool with given arguments and context.

        Args:
            arguments: Tool arguments from LLM
            context: Execution context (repo_root, console, etc.)

        Returns:
            Tool result string with exit_code=N prefix

        Raises:
            RuntimeError: If no handler is registered
        """
        if self.handler is None:
            raise RuntimeError(f"Tool '{self.name}' has no registered handler")

        # Work on a copy so the caller's dict is not mutated
        arguments = dict(arguments)

        # Get the handler's parameter names
        sig = inspect.signature(self.handler)
        handler_params = set(sig.parameters.keys())

        # Inject context parameters only if the handler expects them
        for key, value in context.items():
            if key not in arguments and key in handler_params:
                arguments[key] = value

        return self.handler(**arguments)


# Named groups of related tools for bulk enable/disable
TOOL_GROUPS = {
    "core": {
        "label": "Core",
        "tools": [
            "rg", "read_file", "create_file", "edit_file", "list_directory",
            "execute_command", "web_search", "sub_agent", "search_plugins",
            "select_option", "create_task_list", "complete_task", "show_task_list",
        ],
    },
}


class ToolRegistry:
    """Global registry for all tools.

    Singleton pattern ensures all tools are registered in one place.
    """

    _instance: Optional['ToolRegistry'] = None
    _tools: Dict[str, ToolDefinition] = {}
    _disabled: Dict[str, None] = {}  # dict used as ordered set — mirrors _tools pattern

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def disable(cls, name: str) -> bool:
        """Disable a tool by name.

        Args:
            name: Tool name to disable

        Returns:
            True if name was recognized and disabled
        """
        if name in cls._tools:
            cls._disabled[name] = None
            return True
        return False

    @classmethod
    def enable(cls, name: str) -> bool:
        """Enable a previously disabled tool or plugin.

        Args:
            name: Tool or plugin name to enable

        Returns:
            True if it was disabled and is now re-enabled
        """
        if name in cls._disabled:
            del cls._disabled[name]
            return True
        return False

    @classmethod
    def is_disabled(cls, name: str) -> bool:
        """Check if a tool is currently disabled.

        Args:
            name: Tool name

        Returns:
            True if tool is disabled
        """
        return name in cls._disabled

    @classmethod
    def disable_group(cls, group_key: str) -> list:
        """Disable all tools in a named group.

        Args:
            group_key: Key from TOOL_GROUPS (e.g. "file_ops")

        Returns:
            List of tool names that were actually disabled
        """
        group = TOOL_GROUPS.get(group_key)
        if not group:
            return []
        disabled = []
        for name in group["tools"]:
            if name in cls._tools and name not in cls._disabled:
                cls._disabled[name] = None
                disabled.append(name)
        return disabled

    @classmethod
    def enable_group(cls, group_key: str) -> list:
        """Enable all tools in a named group.

        Args:
            group_key: Key from TOOL_GROUPS (e.g. "file_ops")

        Returns:
            List of tool names that were actually re-enabled
        """
        group = TOOL_GROUPS.get(group_key)
        if not group:
            return []
        enabled = []
        for name in group["tools"]:
            if name in cls._disabled:
                del cls._disabled[name]
                enabled.append(name)
        return enabled

    @classmethod
    def get_group_status(cls, group_key: str) -> dict:
        """Get enabled/disabled status for all tools in a group.

        Args:
            group_key: Key from TOOL_GROUPS

        Returns:
            Dict with 'label', 'tools' list of {name, enabled} dicts
        """
        group = TOOL_GROUPS.get(group_key)
        if not group:
            return {"label": group_key, "tools": []}
        return {
            "label": group["label"],
            "tools": [
                {"name": name, "enabled": name not in cls._disabled}
                for name in group["tools"]
            ],
        }

    @classmethod
    def get_disabled(cls) -> set:
        """Get the set of disabled tool names.

        Returns:
            Set of disabled tool names
        """
        return set(cls._disabled)

    @classmethod
    def register(cls, tool_def: ToolDefinition) -> None:
        """Register a tool definition.

        Args:
            tool_def: ToolDefinition to register

        Note:
            Overwrites existing tools with same name (logs warning)
        """
        if tool_def.name in cls._tools:
            import warnings
            warnings.warn(
                f"Tool '{tool_def.name}' is being overwritten. "
                f"Previous tool: {cls._tools[tool_def.name].handler}, "
                f"New tool: {tool_def.handler}"
            )
        cls._tools[tool_def.name] = tool_def

    @classmethod
    def get(cls, name: str) -> Optional[ToolDefinition]:
        """Get a tool definition by name.

        Args:
            name: Tool name

        Returns:
            ToolDefinition or None if not found
        """
        return cls._tools.get(name)

    @classmethod
    def get_all(cls) -> List[ToolDefinition]:
        """Get all registered and enabled tools."""
        return [t for t in cls._tools.values() if t.name not in cls._disabled]

    @classmethod
    def unregister(cls, name: str) -> bool:
        """Remove a tool from the registry by name.

        Args:
            name: Tool name to remove

        Returns:
            True if tool was found and removed, False if not registered
        """
        cls._disabled.pop(name, None)
        return cls._tools.pop(name, None) is not None

    @classmethod
    def clear(cls) -> None:
        """Clear all registered tools (mainly for testing)."""
        cls._tools.clear()
        cls._disabled.clear()

    @classmethod
    def tool_count(cls) -> int:
        """Get the number of active (enabled) tools in the registry.

        Returns:
            Number of enabled tools (excludes disabled tools)
        """
        return sum(1 for name in cls._tools if name not in cls._disabled)



def get_terminal_policy(tool_name: str) -> str:
    """Get the terminal policy for a tool.

    Args:
        tool_name: Name of the tool

    Returns:
        Terminal policy string (TERMINAL_NONE, TERMINAL_YIELD, or TERMINAL_STOP)
    """
    tool_def = ToolRegistry.get(tool_name)
    if tool_def:
        return tool_def.terminal_policy
    return TERMINAL_NONE  # Default to none for unknown tools


def tool(
    name: str,
    description: str,
    parameters: Dict[str, Any],
    requires_approval: bool = False,
    terminal_policy: str = TERMINAL_NONE,
) -> Callable:
    """Decorator for registering tool functions.

    Usage:
        @tool(
            name="my_tool",
            description="Does something useful",
            parameters={
                "type": "object",
                "properties": {
                    "input": {"type": "string", "description": "Input value"}
                },
                "required": ["input"]
            },
        )
        def my_tool(input: str, repo_root: Path):
            return f"exit_code=0\nProcessed: {input}"

    Args:
        name: Tool identifier
        description: Human-readable description
        parameters: JSON Schema for parameters
        requires_approval: Whether confirmation is required (default: False)
        terminal_policy: Terminal policy for thinking indicator (default: TERMINAL_NONE)

    Returns:
        Decorator function

    Note:
        The decorated function should return a string with exit_code=N prefix,
        e.g., "exit_code=0\nResult content here"
    """
    def decorator(func: Callable) -> Callable:
        # Create tool definition
        tool_def = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            requires_approval=requires_approval,
            terminal_policy=terminal_policy,
            handler=func,
        )

        ToolRegistry.register(tool_def)

        @wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        return wrapper

    return decorator


def build_context(
    repo_root: Path,
    console: Any = None,
    gitignore_spec: Any = None,
    debug_mode: bool = False,
    chat_manager: Any = None,
    rg_exe_path: str = None,
    panel_updater: Any = None,
    vault_root: str = None
) -> Dict[str, Any]:
    """Build execution context for tool invocation.

    Args:
        repo_root: Repository root directory
        console: Rich console for output
        gitignore_spec: PathSpec for .gitignore filtering
        debug_mode: Whether debug mode is enabled
        chat_manager: ChatManager instance
        rg_exe_path: Path to rg executable
        panel_updater: Optional SubAgentPanel for live updates
        vault_root: Optional Obsidian vault root path

    Returns:
        Context dictionary
    """
    context = {
        "repo_root": repo_root,
        "console": console,
        "gitignore_spec": gitignore_spec,
        "debug_mode": debug_mode
    }
    if chat_manager is not None:
        context["chat_manager"] = chat_manager
    if rg_exe_path is not None:
        context["rg_exe_path"] = rg_exe_path
    if panel_updater is not None:
        context["panel_updater"] = panel_updater
    if vault_root is not None:
        context["vault_root"] = vault_root
    return context


# =============================================================================
# Tool schema exports for OpenAI function calling
# =============================================================================

def get_tool_schemas() -> list:
    """Generate OpenAI tool schemas from registry.

    Returns:
        List of tool schemas in OpenAI function-calling format
    """
    return [tool.to_openai_schema() for tool in ToolRegistry.get_all()]


def TOOLS():
    """Get tool schemas. Callable for backward compatibility."""
    return get_tool_schemas()
