"""Capability manifest for on-demand plugin and skill discovery.

Plugin-tier tools are registered here instead of ToolRegistry at import time.
This keeps plugin schemas out of the LLM context window until explicitly
activated via the search_plugins core tool.
"""

import logging
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from core.skills import (
    SearchCandidate,
    iter_skill_summaries,
    search_candidates,
)

from .base import ToolDefinition

logger = logging.getLogger(__name__)


@dataclass
class CapabilityMatch:
    kind: str
    name: str
    description: str
    category: str | None = None
    tags: list[str] | None = None
    tool_def: ToolDefinition | None = None
    preview: str | None = None
    activated: bool = False
    already_active: bool = False


class PluginManifest:
    """Index of plugin-tier tools and stored skills for discovery surfaces.

    Plugin tools are registered here when modules with @tool(tier="plugin")
    are imported. Stored skills are discovered from disk on demand. The
    search_plugins core tool queries this manifest to find and activate or
    load capabilities.
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

    def _iter_capabilities(self, category: str = None) -> Iterable[CapabilityMatch]:
        """Yield available plugin and skill capabilities for discovery surfaces."""
        include_plugins = category in (None, "plugin")
        include_skills = category in (None, "skill")

        if include_plugins:
            for tool_def in self._plugins.values():
                if category not in (None, "plugin") and tool_def.category != category:
                    continue
                yield CapabilityMatch(
                    kind="plugin",
                    name=tool_def.name,
                    description=tool_def.description,
                    category=tool_def.category,
                    tags=list(tool_def.tags or []),
                    tool_def=tool_def,
                )

        if include_skills:
            for summary in iter_skill_summaries():
                yield CapabilityMatch(
                    kind="skill",
                    name=summary.name,
                    description=summary.preview,
                    category="skill",
                    tags=["skill"],
                    preview=summary.preview,
                )

    def search_capabilities(
        self,
        query: str,
        category: str = None,
        max_results: int = 5,
    ) -> List[CapabilityMatch]:
        """Search plugins and skills through one shared discovery path."""
        combined_candidates = [
            SearchCandidate(
                item=capability,
                text=" ".join(
                    part
                    for part in [
                        capability.name,
                        capability.description,
                        capability.category or "",
                        " ".join(capability.tags or []),
                    ]
                    if part
                ),
                compact_text="",
                exact_text=capability.name,
            )
            for capability in self._iter_capabilities(category=category)
        ]
        combined_matches = search_candidates(
            query,
            combined_candidates,
            max_results=max_results,
            item_key=lambda capability: f"{capability.kind}:{capability.name}",
        )
        return [match.item for match in combined_matches]

    def list_all_capabilities(self, category: str = None) -> List[CapabilityMatch]:
        """Return all available capabilities without fuzzy scoring."""
        return list(self._iter_capabilities(category=category))

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
