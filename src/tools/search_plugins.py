"""search_plugins core tool for on-demand capability discovery.

This tool lets the LLM agent search for available plugin tools and
stored skills, then explicitly activate plugins or load skills through
the same entrypoint. Plugin schemas are not sent by default to avoid
context bloat — they are only included after activation.
"""

from tools.helpers.base import tool, ToolRegistry, TERMINAL_NONE

HEADER_MATCHES = "Capability matches for: "
HEADER_ALL = "All available capabilities"


@tool(
    name="search_plugins",
    description=(
        "Search for available plugin tools and saved skills that can help "
        "with your task. Plugins are NOT in your available tools by default "
        "— use this to discover and activate them. Skills can also be loaded "
        "through this same tool by passing explicit capability names in 'load'. "
        "Once a plugin is activated, its full schema will be available in your "
        "next response."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query describing what you need (e.g., 'send email', 'query database', 'http request'). Omit to list all available plugins and skills."
            },
            "load": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of exact capability names from the current search results to activate or load. Plugins are activated; skills are injected into the current chat."
            }
        },
        "required": []
    },
    requires_approval=False,
    terminal_policy=TERMINAL_NONE,
    tier="core",
    tags=["plugin", "discovery", "meta"],
    category="core"
)
def search_plugins(
    query: str = "",
    load: list[str] | None = None,
    chat_manager=None,
) -> str:
    """Search discoverable capabilities and optionally activate/load selected matches.

    When using `load`, `query` must be provided so selections come from the current search results."""
    from core.skills import SkillError, activate_skill, validate_skill_name
    from tools.helpers.plugin_manifest import plugin_manifest

    core_tools = ToolRegistry.get_all(include_plugins=False)
    core_tool_note = None
    query = query.strip()

    if load and not query:
        return "\n".join([
            "exit_code=1",
            "Loading capabilities requires a query so selections come from the current search results.",
        ])

    # No query → list everything
    if not query:
        matches = plugin_manifest.list_all_capabilities()
    else:
        query_lower = query.lower()
        for ct in core_tools:
            if query_lower == ct.name.lower():
                core_tool_note = (
                    f"Core tool already available: {ct.name}\n"
                    f"  {ct.description}"
                )
                break

        matches = plugin_manifest.search_capabilities(query, max_results=10)

    if not matches:
        lines = ["exit_code=0"]
        if core_tool_note:
            lines.extend([core_tool_note, ""])
        if query:
            lines.append(f"No matches for: {query}")
        else:
            lines.append("No plugins or skills available.")
        return "\n".join(lines)

    requested = [name for name in (load or []) if isinstance(name, str) and name.strip()]
    requested_normalized = {name.strip().lower(): name.strip() for name in requested}
    matched_by_name = {match.name.lower(): match for match in matches}

    plugin_count = 0
    skill_count = 0
    loaded_plugins = []
    loaded_skills = []
    load_errors = []

    for match in matches:
        if match.kind == "plugin" and match.tool_def:
            plugin_count += 1
            if ToolRegistry.is_plugin_active(match.tool_def.name):
                match.already_active = True
            if match.name.lower() in requested_normalized and not match.already_active:
                ToolRegistry.activate_plugin(match.tool_def)
                match.activated = True
                loaded_plugins.append(match.name)
            continue
        skill_count += 1
        if match.name.lower() in requested_normalized:
            if chat_manager is None:
                load_errors.append(f"Skill '{match.name}' cannot be loaded without an active chat.")
                continue
            try:
                skill_name = validate_skill_name(match.name)
                activate_skill(chat_manager, skill_name)
                loaded_skills.append(skill_name)
            except SkillError as exc:
                load_errors.append(str(exc))

    missing_requested = [
        original_name
        for normalized_name, original_name in requested_normalized.items()
        if normalized_name not in matched_by_name
    ]
    for missing in missing_requested:
        load_errors.append(f"Capability '{missing}' was not found in the current search results.")

    lines = ["exit_code=0"]
    if core_tool_note:
        lines.extend([core_tool_note, ""])

    if query:
        lines.extend([
            f"{HEADER_MATCHES}{query}",
            f"Results: {len(matches)} total ({plugin_count} plugin, {skill_count} skill)",
            "",
        ])
    else:
        lines.extend([
            HEADER_ALL,
            f"Total: {len(matches)} ({plugin_count} plugin, {skill_count} skill)",
            "",
        ])

    for match in matches:
        if match.kind == "plugin":
            status = "activated" if match.activated else "active" if match.already_active else "available"
            lines.append(f"- {match.name}")
            lines.append("  type: plugin")
            lines.append(f"  status: {status}")
            lines.append(f"  summary: {match.description}")
            if match.tags:
                lines.append(f"  tags: {', '.join(match.tags)}")
            continue

        lines.append(f"- {match.name}")
        lines.append("  type: skill")
        lines.append(f"  summary: {match.description}")
        if match.tags:
            lines.append(f"  tags: {', '.join(match.tags)}")

    if requested:
        lines.append("")
        if loaded_plugins:
            lines.append(f"Activated plugins: {', '.join(loaded_plugins)}")
        if loaded_skills:
            lines.append(f"Loaded skills: {', '.join(loaded_skills)}")
        if load_errors:
            lines.append(f"Load issues: {'; '.join(load_errors)}")

    return "\n".join(lines)
