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
        tier: "core" (always in context) or "plugin" (on-demand via search_plugins)
        tags: List of searchable tags for plugin discovery
        category: Category grouping for plugin discovery (e.g., "email", "database")
    """
    name: str
    description: str
    parameters: Dict[str, Any]
    requires_approval: bool = False
    terminal_policy: str = TERMINAL_NONE  # Default: indicator keeps running
    handler: Optional[Callable] = None
    tier: str = "core"
    tags: List[str] = field(default_factory=list)
    category: str = ""

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
    _plugin_ttl: Dict[str, int] = {}  # tracks remaining turns for activated plugins
    _default_plugin_ttl: int = 10

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def _is_known_name(cls, name: str) -> bool:
        """Check if a name belongs to a registered tool or known plugin."""
        if name in cls._tools:
            return True
        # Lazy check against plugin manifest for names not yet activated
        from tools.helpers.plugin_manifest import plugin_manifest
        return plugin_manifest.has_plugin(name)

    @classmethod
    def disable(cls, name: str) -> bool:
        """Disable a tool or plugin by name.

        Works for core tools (in _tools) and plugins (known via manifest
        but not yet activated).

        Args:
            name: Tool or plugin name to disable

        Returns:
            True if name was recognized and disabled
        """
        if name in cls._tools or cls._is_known_name(name):
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
    def get_all(cls, include_plugins: bool = False) -> List[ToolDefinition]:
        """Get all registered and enabled tools.

        Args:
            include_plugins: If True, include plugin-tier tools. Default: core only.

        Returns:
            List of all ToolDefinitions (excluding disabled, excluding plugins unless requested)
        """
        tools = [t for t in cls._tools.values() if t.name not in cls._disabled]
        if not include_plugins:
            tools = [t for t in tools if t.tier != "plugin"]
        return tools

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
        cls._plugin_ttl.clear()

    @classmethod
    def tool_count(cls) -> int:
        """Get the number of active (enabled) tools in the registry.

        Returns:
            Number of enabled tools (excludes disabled tools)
        """
        return sum(1 for name in cls._tools if name not in cls._disabled)

    # =========================================================================
    # Plugin activation and TTL management
    # =========================================================================

    @classmethod
    def activate_plugin(cls, tool_def, ttl=None) -> bool:
        """Activate a plugin-tier tool by registering it with a TTL.

        Args:
            tool_def: ToolDefinition with tier="plugin"
            ttl: Number of turns before eviction (default: cls._default_plugin_ttl)

        Returns:
            True if the plugin was activated, False if it is disabled
        """
        if cls.is_disabled(tool_def.name):
            _logger.debug("Skipping activation for disabled plugin: %s", tool_def.name)
            return False
        if ttl is None:
            ttl = cls._default_plugin_ttl
        cls._tools[tool_def.name] = tool_def
        cls._plugin_ttl[tool_def.name] = ttl
        _logger.debug(f"Plugin activated: {tool_def.name} (TTL={ttl})")
        return True

    @classmethod
    def deactivate_plugin(cls, name: str) -> bool:
        """Deactivate and remove a plugin-tier tool.

        Args:
            name: Plugin tool name to deactivate

        Returns:
            True if plugin was found and removed
        """
        if name in cls._plugin_ttl:
            del cls._plugin_ttl[name]
        return cls._tools.pop(name, None) is not None

    @classmethod
    def decrement_plugin_ttls(cls):
        """Decrement TTL for all activated plugins. Evict those at zero.

        Returns:
            List of evicted plugin names
        """
        evicted = []
        expired = [name for name, ttl in cls._plugin_ttl.items() if ttl <= 1]
        for name in expired:
            cls.deactivate_plugin(name)
            evicted.append(name)
            _logger.debug(f"Plugin evicted (TTL expired): {name}")
        # Decrement remaining
        for name in cls._plugin_ttl:
            cls._plugin_ttl[name] -= 1
        return evicted

    @classmethod
    def touch_plugin(cls, name: str) -> None:
        """Reset TTL for an activated plugin (called when plugin is used).

        Args:
            name: Plugin tool name
        """
        if name in cls._plugin_ttl:
            cls._plugin_ttl[name] = cls._default_plugin_ttl
            _logger.debug(f"Plugin TTL reset: {name}")

    @classmethod
    def active_plugin_names(cls) -> set:
        """Get the set of currently activated plugin names.

        Returns:
            Set of active plugin tool names
        """
        return set(cls._plugin_ttl.keys())

    @classmethod
    def is_plugin_active(cls, name: str) -> bool:
        """Check if a plugin is currently activated in the registry.

        Args:
            name: Tool name

        Returns:
            True if tool is an active plugin
        """
        return name in cls._plugin_ttl


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


def _enrich_plugin_metadata(tool_def: ToolDefinition) -> None:
    """Auto-generate description and/or tags for a plugin if missing."""
    if tool_def.description and tool_def.tags:
        return

    try:
        source = inspect.getsource(tool_def.handler) if tool_def.handler else ""
    except (OSError, TypeError):
        source = ""

    content = f"{tool_def.name}\n{tool_def.description}\n{source}"
    try:
        from core.metadata import generate_metadata
        generated = generate_metadata(content, tool_def.name)
    except Exception:
        _logger.debug("Plugin metadata enrichment failed for '%s'", tool_def.name, exc_info=True)
        return

    if not tool_def.description:
        tool_def.description = str(generated.get("description", "")).strip()
    if not tool_def.tags:
        raw_tags = generated.get("tags")
        if isinstance(raw_tags, str):
            raw_tags = [raw_tags]
        elif not isinstance(raw_tags, list):
            raw_tags = []
        tool_def.tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()]


def tool(
    name: str,
    description: str,
    parameters: Dict[str, Any],
    requires_approval: bool = False,
    terminal_policy: str = TERMINAL_NONE,
    tier: str = "core",
    tags: Optional[List[str]] = None,
    category: str = ""
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
        tier: "core" or "plugin" (default: "core")
        tags: List of searchable tags for plugin discovery
        category: Category grouping for plugin discovery

    Returns:
        Decorator function

    Note:
        The decorated function should return a string with exit_code=N prefix,
        e.g., "exit_code=0\nResult content here"

        Plugin-tier tools (tier="plugin") are registered in the PluginManifest
        instead of ToolRegistry, so they don't consume context tokens by default.
        They are activated on-demand via the search_plugins core tool.
    """
    def decorator(func: Callable) -> Callable:
        # Validate tier
        if tier not in ("core", "plugin"):
            raise ValueError(
                f"Invalid tier '{tier}' for tool '{name}'. Must be 'core' or 'plugin'."
            )

        # Create tool definition
        tool_def = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            requires_approval=requires_approval,
            terminal_policy=terminal_policy,
            handler=func,
            tier=tier,
            tags=tags or [],
            category=category,
        )

        # Plugin-tier tools go to the manifest, not the registry
        if tier == "plugin":
            _enrich_plugin_metadata(tool_def)
            from .plugin_manifest import plugin_manifest
            plugin_manifest.register(tool_def)
        else:
            # Register core tool normally
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
    return [tool.to_openai_schema() for tool in ToolRegistry.get_all(include_plugins=True)]


def TOOLS():
    """Get tool schemas. Callable for backward compatibility."""
    return get_tool_schemas()
