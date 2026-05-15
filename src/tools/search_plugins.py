"""search_plugins core tool for on-demand skill discovery.

This tool lets the LLM agent search for stored skills and load them
into the current chat session.
"""

from tools.helpers.base import tool, TERMINAL_NONE

HEADER_MATCHES = "Capability matches for: "
HEADER_ALL = "All available capabilities"


@tool(
    name="search_plugins",
    description="Search for available saved skills that can help with your task. Skills are loaded through this tool by passing explicit capability names in 'load'. Once loaded, the skill's instructions are injected into the current chat.",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query describing what you need (e.g., 'code review', 'git workflow'). Omit to list all available skills."
            },
            "load": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of exact skill names from the current search results to load into the current chat."
            }
        },
        "required": []
    },
    requires_approval=False,
    terminal_policy=TERMINAL_NONE,
)
def search_plugins(
    query: str = "",
    load: list[str] | None = None,
    chat_manager=None,
) -> str:
    """Search stored skills and optionally load selected matches."""
    from core.skills import SkillError, activate_skill, validate_skill_name, iter_skill_summaries, search_candidates, SearchCandidate
    from utils.settings import tool_settings

    query = query.strip()
    hidden_skills = set(tool_settings.hidden_skills)

    if load and not query:
        return "\n".join([
            "exit_code=1",
            "Loading skills requires a query so selections come from the current search results.",
        ])

    if not query:
        all_skills = [s for s in iter_skill_summaries() if s.name not in hidden_skills]
        if not all_skills:
            return "exit_code=0\nNo skills available."
        lines = ["exit_code=0", HEADER_ALL, f"Total: {len(all_skills)} skill(s)", ""]
        for skill in sorted(all_skills, key=lambda s: s.name):
            lines.append(f"- {skill.name}")
            lines.append(f"  summary: {skill.description or skill.preview or ''}")
        return "\n".join(lines)

    # Search by query
    candidates = [
        SearchCandidate(
            item=summary,
            text=" ".join(part for part in [summary.name, summary.description or "", summary.preview or ""] if part),
            compact_text="",
            exact_text=summary.name,
        )
        for summary in iter_skill_summaries()
        if summary.name not in hidden_skills
    ]
    results = search_candidates(query, candidates, max_results=10, item_key=lambda s: s.name)
    matches = [r.item for r in results]

    if not matches:
        return f"exit_code=0\nNo skill matches for: {query}"

    # Load requested skills
    requested = [name for name in (load or []) if isinstance(name, str) and name.strip()]
    requested_normalized = {name.strip().lower(): name.strip() for name in requested}
    matched_by_name = {s.name.lower(): s for s in matches}
    loaded_skills = []
    load_errors = []

    for name_lower, original in requested_normalized.items():
        if name_lower not in matched_by_name:
            load_errors.append(f"Skill '{original}' was not found in the current search results.")
            continue
        if chat_manager is None:
            load_errors.append(f"Skill '{original}' cannot be loaded without an active chat.")
            continue
        try:
            skill_name = validate_skill_name(matched_by_name[name_lower].name)
            activate_skill(chat_manager, skill_name)
            loaded_skills.append(skill_name)
        except SkillError as exc:
            load_errors.append(str(exc))

    lines = ["exit_code=0", f"{HEADER_MATCHES}{query}", f"Results: {len(matches)} skill(s)", ""]
    for skill in matches:
        lines.append(f"- {skill.name}")
        lines.append(f"  summary: {skill.description or skill.preview or ''}")

    if requested:
        lines.append("")
        if loaded_skills:
            lines.append(f"Loaded skills: {', '.join(loaded_skills)}")
        if load_errors:
            lines.append(f"Load issues: {'; '.join(load_errors)}")

    return "\n".join(lines)
