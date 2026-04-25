

# Modular prompt composition for native function calling
#
# All prompt sections are loaded from file-based variants under prompts/<variant>/.
# Each variant directory contains individual .md files for each section.
# Section ordering is defined programmatically in _main_sections() and
# _sub_agent_sections() — no manifest files needed.

import logging

logger = logging.getLogger(__name__)
from pathlib import Path
from string import Template

# Root of the prompts directory (repo root / prompts)
_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


# Mode section for main agent

MODE_SECTION = """## Current mode: Edit

**Important:** Explain changes conceptually, show code only in edit tools

Workflow:
1. Analyze request and identify files to modify
2. Generate a brief plan (what/where/why, no code)
3. **Check for trade-offs** - If multiple valid approaches exist, use select_option to clarify
4. Proceed with edits

When the user asks for a plan (e.g. "plan this out", "what's involved", "before you start"):
- Explore and understand requirements first
- Propose a structured plan with bullet points: what changes, where, and why
- Highlight trade-offs and ambiguities using select_option
- End with a summary of the proposed changes
- Ask: 'Do you approve this plan?' before proceeding with edits

Show code only when using `edit_file`/`create_file` tools. Keep text explanations concise."""


# Sub-agent specific sections (research-focused, read-only tools passed via function calling)

SUB_AGENT_SECTIONS = {
    "token_budget": """## Token Budget

You have a total budget of approximately $hard_limit tokens for this task. When you reach $soft_limit tokens, you MUST immediately stop exploring and return your findings to the main agent. Do not continue reading files, searching, or making tool calls once you are near or past the soft limit. Wrap up your answer with citations and return it promptly.""",

    "response_format": """# Response Format

When answering the main agent's query:

1. **Provide a clear summary** of your findings
2. **Cite only the most relevant files with precise line ranges** for code you've actually read

**Important:** Only cite files where you have actually read the content. The main agent will
inject the actual file contents based on your citations and will trust these injected contents
without re-reading them.

**Required:** You must use bracketed citation formats only. Unbracketed formats like `file:N`
will not be recognized and will be ignored.

Use these citation formats:
- `[path/to/file] (lines N-M)` - for a specific range you've fully read (preferred)
- `[path/to/file]:N-M` - bracketed range notation (preferred)
- `[path/to/file]:N` - bracketed single line notation (preferred)
- `[path/to/file] (full)` - only for small files or when you genuinely need the entire file

**Citation Guidelines - Be Selective:**
- Be precise with line numbers - cite only the specific ranges that matter
- Prioritize specific ranges (lines N-M) over full files
- Avoid citing large files with (full) - use specific ranges instead
- Omit boilerplate, tests, and utility code unless directly relevant
- The main agent can always request more context if needed

Example:
"The authentication flow starts in [src/core/auth.py] (lines 45-78) where tokens are validated,
 then calls [src/core/session.py] (lines 112-145) for session management."

The main agent will automatically inject the actual file contents based on your citations,
so the main agent doesn't need to re-read files you've already explored.""",

    "mode": """# Current mode: Research

You are a research sub-agent. Answer the specific question asked — do not explore the whole subsystem. Use read-only tools (rg, read_file, list_directory) to gather just enough information.

**Stop early:** Answer when you can address the query. The main agent can call you again for follow-up if needed. Prefer the most likely paths based on codebase structure.""",

    "review_mode": """# Current mode: Code Review

You are a code review agent. Analyze the provided git diff and provide honest, useful feedback.
Your output goes directly to the user — write clean, readable markdown.

## Workflow
1. Parse file paths from diff headers (`+++ b/` or `--- a/`)
2. Use `read_file` on each changed file for surrounding context
3. Cross-reference related files when needed
4. Write your review

## Output Template

Follow this exact structure. Do not add extra sections or reorder.

### Summary
One paragraph (2-4 sentences). What changed, overall quality. If nothing noteworthy, say so.

### Issues
Group issues by severity under sub-headings. Only include levels that have findings.

#### Critical (N)
- `[path/to/file]:line` — short description

#### Warning (N)
- `[path/to/file]:line` — short description

#### Info (N)
- `[path/to/file]:line` — short description

Severity levels:
- **critical** — Blocking. Must fix before merge. Use sparingly.
- **warning** — Should fix, not blocking.
- **info** — Style, naming, nitpicks.

One bullet per issue. One line each. No paragraphs. Keep descriptions brief.

### Verdict
Always end with a verdict. One line: `APPROVE - explanation` or `REQUEST CHANGES - explanation`.
- `APPROVE` — no critical issues. Mention what looked good or minor nits.
- `REQUEST CHANGES` — critical issues found. Summarize what needs fixing.

## Anti-Fabrication Rule
Do not manufacture issues or inflate severity. If nothing is wrong, say so in the summary and skip those labels. An honest "No issues found" beats a fabricated nitpick. Use bracketed citations: `[path/to/file]:line_number`.""",
}


# Builder functions to compose prompts from sections

# Mapping of prompt section keys to the tool names they depend on.
# If ALL listed tools are disabled, the section is omitted from the prompt.
# Sections not listed have no tool dependency and are always included.
SECTION_TOOL_DEPS = {
    "trust_subagent_context": ["sub_agent"],
    "when_to_use_sub_agent": ["sub_agent"],
    "ask_questions": ["select_option"],
    "editing_pattern": ["edit_file"],
    "task_lists_pattern": ["create_task_list", "complete_task", "show_task_list", "edit_file"],
    "temp_folder": ["create_file"],
}


def _build_memory_section() -> str | None:
    """Build the read-only memory context section for the system prompt.

    Injects live memory content blocks with capacity headers.
    No writing instructions — memory files are read-only during conversations.
    All writes happen through the dream cron job.

    Returns None if MemoryManager is not initialized or memory is disabled.
    """
    from llm.config import MEMORY_SETTINGS
    if not MEMORY_SETTINGS.get("enabled", True):
        return None

    try:
        from core.memory import MemoryManager
        manager = MemoryManager.get_instance()
        if manager is None:
            return None

        result = ""

        # Append capacity headers and memory content if files have real content
        user_content = manager.load_user_memory()
        user_usage = manager.get_user_usage()
        if manager._has_entries(user_content):
            pct = user_usage["chars_used"] * 100 // user_usage["chars_limit"]
            result += f"USER MEMORY [{pct}% — {user_usage['chars_used']}/{user_usage['chars_limit']} chars]\n{user_content.strip()}\n\n"

        project_content = manager.load_project_memory()
        project_usage = manager.get_project_usage()
        if manager._has_entries(project_content):
            pct = project_usage["chars_used"] * 100 // project_usage["chars_limit"]
            result += f"PROJECT MEMORY [{pct}% — {project_usage['chars_used']}/{project_usage['chars_limit']} chars]\n{project_content.strip()}\n\n"

        return result.strip() if result else None
    except Exception:
        return None


def _build_vault_section(variant: str = "main") -> str | None:
    """Build the Obsidian vault section for the system prompt.

    Loads obsidian.md from prompts/<variant>/ and substitutes dynamic values
    (vault root, project folder, excluded folders) using string.Template.
    If project exists, also loads and appends obsidian_project.md from the
    same variant directory.

    Returns None if vault is not active.
    """
    try:
        from utils.settings import obsidian_settings
        if not obsidian_settings.is_active():
            return None
    except Exception as e:
        logger.debug("Obsidian not available: %s", e)
        return None

    try:
        from tools.obsidian import get_vault_session, init_session
        session = get_vault_session()
        # Initialize session on first prompt build if not yet available.
        # Normally initialized by AgenticLoop.__init__, but the system prompt
        # is built earlier (in ChatManager.__init__), causing an inconsistent
        # vault section (missing note schemas) on fresh start.
        if session is None:
            session = init_session()
    except Exception:
        session = None

    vault_root = str(session.vault_root) if session else "<not available>"
    project_folder = str(session.project_folder) if session else "<not available>"

    project_exists = (
        session
        and session.project_folder.is_dir()
        and (session.project_folder / "Bugs").is_dir()
    )

    excluded = obsidian_settings.exclude_folders

    # Load base obsidian template from variant directory
    base_path = _PROMPTS_DIR / variant / "obsidian.md"
    if not base_path.is_file():
        logger.warning(
            "Obsidian template not found at %s — vault section omitted", base_path
        )
        return None

    base_content = base_path.read_text(encoding="utf-8").strip()

    # Substitute dynamic values using string.Template
    if project_exists:
        project_header = f"**Project folder:** `{project_folder}`"
    else:
        project_header = "**Project:** not initialized (run `/obsidian init` to create)"

    formatted = Template(base_content).safe_substitute(
        vault_root=vault_root,
        project_folder=project_folder,
        project_header=project_header,
        excluded=excluded,
    )

    # Append project-specific section if project exists
    if project_exists:
        project_path = _PROMPTS_DIR / variant / "obsidian_project.md"
        if project_path.is_file():
            project_content = project_path.read_text(encoding="utf-8").strip()
            formatted = formatted + "\n\n" + project_content

    return formatted


def _build_context_section() -> str:
    """Build a dynamic section with current date and location."""
    from datetime import datetime
    import os

    now = datetime.now()
    date_str = now.strftime("%A, %B %d, %Y")

    return (
        "## Current Context\n\n"
        f"**Date:** {date_str}\n"
        f"**Working directory:** {os.getcwd()}\n"
    )


def _static(variant: str, name: str) -> str:
    """Load a static .md section from prompts/<variant>/."""
    path = _PROMPTS_DIR / variant / name
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    logger.warning("Section file not found: %s — prompt section '%s' omitted", path, name)
    return ""


def _resolve_variant() -> str:
    """Resolve the prompt variant from settings, falling back to 'main'."""
    try:
        from utils.settings import prompt_settings
        return prompt_settings.variant
    except Exception:
        return "main"


def _should_include_section(section_key: str) -> bool:
    """Check whether a prompt section should be included based on tool availability.

    A section is skipped only when ALL of its dependent tools are disabled.
    Uses lazy import to avoid circular dependency with tools module.
    """
    deps = SECTION_TOOL_DEPS.get(section_key)
    if not deps:
        return True
    from tools.helpers.base import ToolRegistry
    return not all(ToolRegistry.is_disabled(t) for t in deps)


def _build_prompt_to_list(sections: list[tuple[str, callable]]) -> list[str]:
    """Build prompt as a list of section strings from (key, content_fn) pairs.

    Skips sections that fail _should_include_section (tool deps)
    or whose content_fn returns None/empty.
    """
    result = []
    for key, content_fn in sections:
        if not _should_include_section(key):
            continue
        content = content_fn()
        if content:
            result.append(content)
    return result


def _build_prompt(sections: list[tuple[str, callable]]) -> str:
    """Build prompt string from (key, content_fn) pairs.

    Delegates to _build_prompt_to_list and joins the result.
    """
    return "\n\n".join(_build_prompt_to_list(sections))


def _variant_available(variant: str) -> bool:
    """Check if a variant directory exists.

    Raises OSError if directory permissions prevent access.
    """
    return (_PROMPTS_DIR / variant).is_dir()


def _list_variants() -> list[str]:
    """List available prompt variants (directories under prompts/)."""
    if not _PROMPTS_DIR.is_dir():
        return []
    return sorted(
        d.name for d in _PROMPTS_DIR.iterdir()
        if d.is_dir()
    )


def _main_sections(variant: str) -> list[tuple[str, callable]]:
    """Return (key, content_fn) pairs for the main agent prompt.

    Order in this list = order in the final prompt.
    Static .md sections, dynamic builders, and hardcoded sections all
    live here — single source of truth for ordering.
    """
    return [
        ("intro", lambda: _static(variant, "intro.md")),
        ("context", _build_context_section),
        ("tone_and_style", lambda: _static(variant, "tone_and_style.md")),
        ("communication_style", lambda: _static(variant, "communication_style.md")),
        ("trust_subagent_context", lambda: _static(variant, "trust_subagent_context.md")),
        ("context_reliability", lambda: _static(variant, "context_reliability.md")),
        ("skills", lambda: _static(variant, "skills.md")),
        ("conversational_tool_calling", lambda: _static(variant, "conversational_tool_calling.md")),
        ("professional_objectivity", lambda: _static(variant, "professional_objectivity.md")),
        ("think_before_acting", lambda: _static(variant, "think_before_acting.md")),
        ("batch_independent_calls", lambda: _static(variant, "batch_independent_calls.md")),
        ("code_references", lambda: _static(variant, "code_references.md")),
        ("exploration_pattern", lambda: _static(variant, "exploration_pattern.md")),
        ("targeted_searching", lambda: _static(variant, "targeted_searching.md")),
        ("editing_pattern", lambda: _static(variant, "editing_pattern.md")),
        ("task_lists_pattern", lambda: _static(variant, "task_lists_pattern.md")),
        ("casual_interactions", lambda: _static(variant, "casual_interactions.md")),
        ("ask_questions", lambda: _static(variant, "ask_questions.md")),
        ("tool_preferences", lambda: _static(variant, "tool_preferences.md")),
        ("when_to_use_sub_agent", lambda: _static(variant, "when_to_use_sub_agent.md")),
        ("error_handling", lambda: _static(variant, "error_handling.md")),
        ("temp_folder", lambda: _static(variant, "temp_folder.md")),
        ("memory_system", _build_memory_section),
        ("obsidian", lambda: _build_vault_section(variant)),
        ("mode", lambda: MODE_SECTION),
    ]


def _sub_agent_sections(variant: str) -> list[tuple[str, callable]]:
    """Return (key, content_fn) pairs for the sub-agent prompt.

    The micro variant has a smaller set of sections than main.
    response_format is placed explicitly after code_references.
    """
    # Base sections shared across all variants
    base = [
        ("intro", lambda: _static(variant, "intro.md")),
        ("context", _build_context_section),
        ("tone_and_style", lambda: _static(variant, "tone_and_style.md")),
        ("communication_style", lambda: _static(variant, "communication_style.md")),
    ]

    # Micro variant has a different section set (no conversational_tool_calling,
    # no professional_objectivity, no think_before_acting, etc.)
    if variant == "micro":
        middle = [
            ("trust_subagent_context", lambda: _static(variant, "trust_subagent_context.md")),
            ("context_reliability", lambda: _static(variant, "context_reliability.md")),
            ("skills", lambda: _static(variant, "skills.md")),
            ("exploration_pattern", lambda: _static(variant, "exploration_pattern.md")),
            ("targeted_searching", lambda: _static(variant, "targeted_searching.md")),
            ("tool_preferences", lambda: _static(variant, "tool_preferences.md")),
        ]
    else:
        middle = [
            ("skills", lambda: _static(variant, "skills.md")),
            ("conversational_tool_calling", lambda: _static(variant, "conversational_tool_calling.md")),
            ("professional_objectivity", lambda: _static(variant, "professional_objectivity.md")),
            ("think_before_acting", lambda: _static(variant, "think_before_acting.md")),
            ("batch_independent_calls", lambda: _static(variant, "batch_independent_calls.md")),
            ("code_references", lambda: _static(variant, "code_references.md")),
            ("response_format", lambda: SUB_AGENT_SECTIONS["response_format"]),
            ("exploration_pattern", lambda: _static(variant, "exploration_pattern.md")),
            ("targeted_searching", lambda: _static(variant, "targeted_searching.md")),
            ("casual_interactions", lambda: _static(variant, "casual_interactions.md")),
            ("temp_folder", lambda: _static(variant, "temp_folder.md")),
        ]

    return base + middle


def build_system_prompt(variant: str | None = None, active_skills_section: str = "") -> str:
    """Build system prompt for main agent.

    Loads section content from prompts/<variant>/. Order is defined by
    _main_sections(). Raises FileNotFoundError if variant directory is missing.

    Args:
        variant: Variant name (e.g. 'main', 'micro').
            If None, reads from settings.
        active_skills_section: Optional rendered active-skills block to append.

    Returns:
        Complete system prompt string
    """
    if variant is None:
        variant = _resolve_variant()
    if not _variant_available(variant):
        raise FileNotFoundError(
            f"Prompt variant '{variant}' not found: "
            f"{_PROMPTS_DIR / variant} does not exist"
        )
    result = _build_prompt(_main_sections(variant))
    if active_skills_section.strip():
        result += "\n\n" + active_skills_section.strip()
    return result


def build_sub_agent_prompt(sub_agent_type: str = "research", soft_limit_tokens: int | None = None, hard_limit_tokens: int | None = None) -> str:
    """Build prompt for sub-agent (research or review, read-only).

    Args:
        sub_agent_type: Type of sub-agent ('research' or 'review').
        soft_limit_tokens: Soft token limit to display in prompt.
        hard_limit_tokens: Hard token limit to display in prompt.

    Returns:
        Complete system prompt string
    """
    variant = _resolve_variant()
    if not _variant_available(variant):
        raise FileNotFoundError(
            f"Sub-agent prompt variant '{variant}' not found: "
            f"{_PROMPTS_DIR / variant} does not exist"
        )

    result = _build_prompt_to_list(_sub_agent_sections(variant))

    # Append parameterized sections (always last)
    if soft_limit_tokens is not None and hard_limit_tokens is not None:
        result.append(
            Template(SUB_AGENT_SECTIONS["token_budget"]).safe_substitute(
                soft_limit=f"{soft_limit_tokens:,}",
                hard_limit=f"{hard_limit_tokens:,}",
            )
        )

    if sub_agent_type == "review":
        result.append(SUB_AGENT_SECTIONS["review_mode"])
    else:
        result.append(SUB_AGENT_SECTIONS["mode"])

    return "\n\n".join(result)


