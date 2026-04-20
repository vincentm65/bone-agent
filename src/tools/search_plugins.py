"""search_plugins core tool for on-demand plugin discovery.

This tool lets the LLM agent search for available plugin tools and
activate them. Plugin schemas are not sent by default to avoid context
bloat — they are only included after activation.
"""

from typing import List, Optional
from pathlib import Path

from tools.helpers.base import tool, ToolRegistry, TERMINAL_NONE


@tool(
    name="search_plugins",
    description=(
        "Search for available plugin tools that can help with your task. "
        "Plugin tools are NOT in your available tools by default — use this "
        "to discover and activate them. Returns matching plugin names and "
        "descriptions. Once activated, the plugin's full schema will be "
        "available in your next response."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query describing what you need (e.g., 'send email', 'query database', 'http request')"
            },
            "category": {
                "type": "string",
                "description": "Optional category filter (e.g., 'email', 'database', 'analysis')"
            }
        },
        "required": ["query"]
    },
    requires_approval=False,
    terminal_policy=TERMINAL_NONE,
    tier="core",
    tags=["plugin", "discovery", "meta"],
    category="core"
)
def search_plugins(
    query: str,
    category: str = None,
) -> str:
    """Search the plugin manifest and activate matching plugins.

    Args:
        query: Search query describing the needed capability
        category: Optional category filter

    Returns:
        Formatted result with activated plugin names and descriptions
    """
    from tools.helpers.plugin_manifest import plugin_manifest

    # Check if any core tool matches the query — early return
    core_tools = ToolRegistry.get_all(include_plugins=False)
    query_lower = query.lower()
    for ct in core_tools:
        if query_lower == ct.name.lower() or query_lower in ct.name.lower():
            return (
                f"exit_code=0\n"
                f"'{query}' matches a core tool that is already available: **{ct.name}**.\n"
                f"Description: {ct.description}"
            )

    # Search the plugin manifest
    matches = plugin_manifest.search(query, category=category, max_results=5)

    if not matches:
        # No matches — suggest available categories
        categories = plugin_manifest.get_categories()
        if categories:
            cat_list = ", ".join(f"'{c}'" for c in categories)
            return (
                f"exit_code=0\n"
                f"No plugins found matching '{query}'.\n"
                f"Available plugin categories: {cat_list}\n"
                f"Total plugins in manifest: {plugin_manifest.plugin_count()}"
            )
        return (
            f"exit_code=0\n"
            f"No plugins found matching '{query}'. "
            f"No plugins are currently registered in the manifest."
        )

    # Activate matched plugins in the registry
    activated = []
    already_active = []
    for tool_def in matches:
        if ToolRegistry.is_plugin_active(tool_def.name):
            already_active.append(tool_def.name)
        else:
            ToolRegistry.activate_plugin(tool_def)
            activated.append(tool_def.name)

    # Build result
    lines = [f"exit_code=0\nFound {len(matches)} plugin(s) matching '{query}':\n"]

    for tool_def in matches:
        status = "activated" if tool_def.name in activated else "already active"
        cat_part = f" [{tool_def.category}]" if tool_def.category else ""
        lines.append(f"- **{tool_def.name}**{cat_part} ({status}): {tool_def.description}")
        if tool_def.tags:
            lines.append(f"  Tags: {', '.join(tool_def.tags)}")

    return "\n".join(lines)
