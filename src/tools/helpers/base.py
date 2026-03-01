"""Tool registry and decorator for automatic tool registration.

This module provides the core infrastructure for defining tools with a
decorator-based pattern. Tools are automatically registered and can be
filtered by interaction mode.
"""

import inspect
from typing import Dict, List, Optional, Callable, Any, Set
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path


@dataclass
class ToolDefinition:
    """Definition of a tool including metadata and execution handler.

    Attributes:
        name: Tool identifier (e.g., "read_file")
        description: Human-readable description of what the tool does
        parameters: JSON Schema for tool parameters
        allowed_modes: List of interaction modes where this tool is allowed
        requires_approval: Whether this tool requires user confirmation
        handler: Function that executes the tool
    """
    name: str
    description: str
    parameters: Dict[str, Any]
    allowed_modes: List[str] = field(default_factory=lambda: ["edit", "plan", "learn"])
    requires_approval: bool = False
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

    def is_allowed_in_mode(self, mode: str) -> bool:
        """Check if tool is allowed in given interaction mode.

        Args:
            mode: Interaction mode ('edit', 'plan', or 'learn')

        Returns:
            True if tool is allowed in this mode
        """
        return mode in self.allowed_modes

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

        # Get the handler's parameter names
        sig = inspect.signature(self.handler)
        handler_params = set(sig.parameters.keys())

        # Inject context parameters only if the handler expects them
        for key, value in context.items():
            if key not in arguments and key in handler_params:
                arguments[key] = value

        return self.handler(**arguments)


class ToolRegistry:
    """Global registry for all tools.

    Singleton pattern ensures all tools are registered in one place.
    """

    _instance: Optional['ToolRegistry'] = None
    _tools: Dict[str, ToolDefinition] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

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
        """Get all registered tools.

        Returns:
            List of all ToolDefinitions
        """
        return list(cls._tools.values())

    @classmethod
    def get_tools_for_mode(cls, mode: str) -> List[ToolDefinition]:
        """Get tools allowed in a specific interaction mode.

        Args:
            mode: Interaction mode ('edit', 'plan', or 'learn')

        Returns:
            List of ToolDefinitions for that mode
        """
        return [tool for tool in cls._tools.values() if tool.is_allowed_in_mode(mode)]

    @classmethod
    def clear(cls) -> None:
        """Clear all registered tools (mainly for testing)."""
        cls._tools.clear()

    @classmethod
    def tool_count(cls) -> int:
        """Get the total number of registered tools.

        Returns:
            Number of tools in registry
        """
        return len(cls._tools)


def tool(
    name: str,
    description: str,
    parameters: Dict[str, Any],
    allowed_modes: Optional[List[str]] = None,
    requires_approval: bool = False
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
            allowed_modes=["edit"]
        )
        def my_tool(input: str, repo_root: Path):
            return f"exit_code=0\nProcessed: {input}"

    Args:
        name: Tool identifier
        description: Human-readable description
        parameters: JSON Schema for parameters
        allowed_modes: List of allowed modes (default: all modes)
        requires_approval: Whether confirmation is required (default: False)

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
            allowed_modes=allowed_modes or ["edit", "plan", "learn"],
            requires_approval=requires_approval,
            handler=func
        )

        # Register tool
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
    interaction_mode: str = "edit",
    chat_manager: Any = None,
    rg_exe_path: str = None,
    panel_updater: Any = None
) -> Dict[str, Any]:
    """Build execution context for tool invocation.

    Args:
        repo_root: Repository root directory
        console: Rich console for output
        gitignore_spec: PathSpec for .gitignore filtering
        debug_mode: Whether debug mode is enabled
        interaction_mode: Current interaction mode
        chat_manager: ChatManager instance
        rg_exe_path: Path to rg executable
        panel_updater: Optional SubAgentPanel for live updates

    Returns:
        Context dictionary
    """
    context = {
        "repo_root": repo_root,
        "console": console,
        "gitignore_spec": gitignore_spec,
        "debug_mode": debug_mode,
        "interaction_mode": interaction_mode
    }
    if chat_manager is not None:
        context["chat_manager"] = chat_manager
    if rg_exe_path is not None:
        context["rg_exe_path"] = rg_exe_path
    if panel_updater is not None:
        context["panel_updater"] = panel_updater
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


def get_tools_for_mode(interaction_mode: str) -> list:
    """Get tool schemas filtered by interaction mode.

    Args:
        interaction_mode: 'plan', 'edit', or 'learn'

    Returns:
        List of tool schemas suitable for the mode
    """
    return [
        tool.to_openai_schema()
        for tool in ToolRegistry.get_tools_for_mode(interaction_mode)
    ]


# Export TOOLS as a callable that ensures it always reflects the current
# state of the registry (which is populated at runtime after tool modules are imported).
TOOLS = get_tool_schemas


def _tools_for_mode(interaction_mode):
    """Filter tools based on interaction mode using the registry.

    Args:
        interaction_mode: 'plan', 'edit', or 'learn'

    Returns:
        List of tool definitions suitable for the mode
    """
    return get_tools_for_mode(interaction_mode)
