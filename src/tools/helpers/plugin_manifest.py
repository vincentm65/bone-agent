"""Plugin manifest for on-demand tool discovery.

Plugin-tier tools are registered here instead of ToolRegistry at import time.
This keeps plugin schemas out of the LLM context window until explicitly
activated via the search_plugins core tool.
"""

import logging
from typing import Dict, List, Optional

from .base import ToolDefinition

logger = logging.getLogger(__name__)


class PluginManifest:
    """Index of plugin-tier tools available for on-demand activation.

    Tools are registered here when modules with @tool(tier="plugin") are
    imported. The search_plugins core tool queries this index to find and
    activate matching plugins.
    """

    def __init__(self):
        self._plugins: Dict[str, ToolDefinition] = {}

    def register(self, tool_def: ToolDefinition) -> None:
        """Register a plugin tool definition.

        Args:
            tool_def: ToolDefinition with tier="plugin"
        """
        if tool_def.name in self._plugins:
            logger.warning(
                f"Plugin '{tool_def.name}' is being overwritten. "
                f"Previous: {self._plugins[tool_def.name].handler}, "
                f"New: {tool_def.handler}"
            )
        self._plugins[tool_def.name] = tool_def
        logger.debug(f"Plugin registered in manifest: {tool_def.name}")

    def get(self, name: str) -> Optional[ToolDefinition]:
        """Get a plugin tool definition by name.

        Args:
            name: Plugin tool name

        Returns:
            ToolDefinition or None if not found
        """
        return self._plugins.get(name)

    def get_all(self) -> List[ToolDefinition]:
        """Get all registered plugin definitions.

        Returns:
            List of all ToolDefinitions in the manifest
        """
        return list(self._plugins.values())

    def search(self, query: str, category: str = None, max_results: int = 5) -> List[ToolDefinition]:
        """Search the manifest for plugins matching a query.

        Args:
            query: Search query (matched against name, description, tags, category)
            category: Optional category filter
            max_results: Maximum number of results to return

        Returns:
            List of matching ToolDefinitions, sorted by relevance score
        """
        query_lower = query.lower()
        query_terms = query_lower.split()

        scored = []
        for tool_def in self._plugins.values():
            # Apply category filter if specified
            if category and tool_def.category != category:
                continue

            # Calculate relevance score
            score = 0.0

            # Exact name match (highest priority)
            if query_lower == tool_def.name.lower():
                score += 100.0

            # Name contains query
            if query_lower in tool_def.name.lower():
                score += 50.0

            # Name contains individual terms
            for term in query_terms:
                if term in tool_def.name.lower():
                    score += 20.0

            # Description match
            if query_lower in tool_def.description.lower():
                score += 30.0
            for term in query_terms:
                if term in tool_def.description.lower():
                    score += 10.0

            # Tag match
            for tag in tool_def.tags:
                if query_lower in tag.lower():
                    score += 15.0
                for term in query_terms:
                    if term in tag.lower():
                        score += 5.0

            # Category match
            if category and tool_def.category == category:
                score += 10.0
            elif not category and query_lower in tool_def.category.lower():
                score += 15.0

            if score > 0:
                scored.append((score, tool_def))

        # Sort by score descending
        scored.sort(key=lambda x: x[0], reverse=True)

        return [tool_def for _, tool_def in scored[:max_results]]

    def get_categories(self) -> List[str]:
        """Get all unique categories in the manifest.

        Returns:
            Sorted list of category strings
        """
        categories = {td.category for td in self._plugins.values() if td.category}
        return sorted(categories)

    def plugin_count(self) -> int:
        """Get the number of registered plugins.

        Returns:
            Number of plugins in the manifest
        """
        return len(self._plugins)

    def has_plugin(self, name: str) -> bool:
        """Check if a plugin exists in the manifest.

        Args:
            name: Plugin tool name

        Returns:
            True if plugin is in the manifest
        """
        return name in self._plugins


# Singleton instance
plugin_manifest = PluginManifest()
