

# Modular prompt composition for native function calling
#
# Prompt sections are loaded from prompts/micro/*.md files.
# Section ordering is defined programmatically in _main_sections() and
# _sub_agent_sections() — no manifest files needed.

import logging

logger = logging.getLogger(__name__)
from pathlib import Path
from string import Template

# Root of the prompts directory (repo root / prompts)
_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


# Sub-agent specific sections (research-focused, read-only tools passed via function calling)

SUB_AGENT_SECTIONS = {
    "token_budget": """## Token Budget

You have a total budget of approximately $hard_limit tokens for this task. If you have enough evidence to answer, stop exploring and return findings to the main agent. Wrap up with citations promptly.""",

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

You are a bounded research sub-agent. Answer only the delegated question; do not complete the whole user task or map a subsystem unless explicitly asked.

Use read-only tools to gather just enough evidence: start with targeted rg, read only the most relevant files/ranges, and avoid reading every search result or large files in full. Stop once you can answer; the main agent can ask follow-ups.

**read_file discipline:**
- Always provide start_line and max_lines. Never read an entire file unless you already know it is small (<50 lines).
- Use rg to locate relevant line numbers first, then read only the range you need.
- If rg returns matches, read 20-50 lines of context around each match — not the whole file.
- Reading an entire 500+ line file is almost always a mistake that wastes your token budget.

Return a compact research packet: direct answer, key files/functions, minimal citations, and any important uncertainty.""",

    "review_mode": """# Current mode: Code Review

You are a code review agent. Analyze the provided git diff and provide honest, useful feedback.
Your output goes directly to the user — write clean, readable markdown.

## Completeness
Report every issue you find, not a sample. If you found 20 warnings, list all 20. Do not stop early or summarize counts without listing each finding.

## Workflow
The full diff is embedded below in a `## Git Diff` section. You already have all changed lines — do not re-read them.
1. Read the embedded diff carefully
2. Write your review from the diff alone — you have the code
3. Only use `read_file` if you genuinely cannot assess an issue from the diff (e.g., you need to see a caller, a base class, or surrounding context that was not changed). Limit yourself to 2-3 `read_file` calls total.

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
- **critical** — Unexpected runtime behavior: crashes, exceptions, data corruption, wrong results. Must be reproducible with a specific code path. Use sparingly.
- **warning** — Clean-up items: logging issues, missing error handling that degrades quality, resource leaks, minor logic errors that produce suboptimal results.
- **info** — Notes, style, naming, suggestions. Not bugs.

One bullet per issue. One line each. No paragraphs. Keep descriptions brief.

### Verdict
Always end with a verdict. One line: `APPROVE - explanation` or `REQUEST CHANGES - explanation`.
- `APPROVE` — no critical issues. Mention what looked good or minor nits.
- `REQUEST CHANGES` — critical issues found. Summarize what needs fixing.

## Anti-Fabrication Rule
Do not manufacture issues or inflate severity. Only report issues you can trace to a specific code path — if you can't explain how to trigger it, it's not a finding. Don't flag something as a bug because it looks unusual; flag it because you can show what goes wrong. If nothing is wrong, say so in the summary and skip those labels. An honest "No issues found" beats a fabricated nitpick. Use bracketed citations: `[path/to/file]:line_number`.""",
}


# Builder functions to compose prompts from sections


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


def _build_vault_section() -> str | None:
    """Build the Obsidian vault section for the system prompt.

    Loads obsidian.md from prompts/micro/ and substitutes dynamic values
    (vault root, project folder, excluded folders) using string.Template.
    If project exists, also loads and appends obsidian_project.md.

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

    # Load base obsidian template
    base_path = _PROMPTS_DIR / "micro" / "obsidian.md"
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
        project_path = _PROMPTS_DIR / "micro" / "obsidian_project.md"
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


def _static(name: str) -> str:
    """Load a static .md section from prompts/micro/."""
    path = _PROMPTS_DIR / "micro" / name
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    logger.warning("Section file not found: %s — prompt section '%s' omitted", path, name)
    return ""


def _build_prompt_to_list(sections: list[tuple[str, callable]]) -> list[str]:
    """Build prompt as a list of section strings from (key, content_fn) pairs.
    Skips sections whose content_fn returns None/empty.
    """
    result = []
    for key, content_fn in sections:
        content = content_fn()
        if content:
            result.append(content)
    return result


def _build_prompt(sections: list[tuple[str, callable]]) -> str:
    """Build prompt string from (key, content_fn) pairs.

    Delegates to _build_prompt_to_list and joins the result.
    """
    return "\n\n".join(_build_prompt_to_list(sections))


def _main_sections() -> list[tuple[str, callable]]:
    """Return (key, content_fn) pairs for the main agent prompt."""
    return [
        ("context", _build_context_section),
        ("skills", lambda: _static("skills.md")),
        ("cron", lambda: _static("cron.md")),
        ("memory_system", _build_memory_section),
        ("obsidian", _build_vault_section),
        ("temp_folder", lambda: _static("temp_folder.md")),
    ]


def _sub_agent_sections() -> list[tuple[str, callable]]:
    """Return (key, content_fn) pairs for the sub-agent prompt."""
    return [
        ("context", _build_context_section),
    ]


def build_system_prompt(active_skills_section: str = "") -> str:
    """Build system prompt for main agent.

    Loads section content from prompts/micro/. Order is defined by
    _main_sections().

    Args:
        active_skills_section: Optional rendered active-skills block to append.

    Returns:
        Complete system prompt string
    """
    result = _build_prompt(_main_sections())
    if active_skills_section.strip():
        result += "\n\n" + active_skills_section.strip()
    return result


def build_sub_agent_prompt(sub_agent_type: str = "research", hard_limit_tokens: int | None = None, diff_content: str | None = None) -> str:
    """Build prompt for sub-agent (research or review, read-only).

    Args:
        sub_agent_type: Type of sub-agent ('research' or 'review').
        hard_limit_tokens: Hard token limit to display in prompt.
        diff_content: Optional git diff to embed directly in the system prompt
            (review mode). Avoids wasting a user-message turn on raw diff text.

    Returns:
        Complete system prompt string
    """
    result = _build_prompt_to_list(_sub_agent_sections())

    # Append parameterized sections (always last)
    if hard_limit_tokens is not None:
        result.append(
            Template(SUB_AGENT_SECTIONS["token_budget"]).safe_substitute(
                hard_limit=f"{hard_limit_tokens:,}",
            )
        )

    if sub_agent_type == "review":
        result.append(SUB_AGENT_SECTIONS["review_mode"])
        if diff_content:
            result.append(f"## Git Diff\n\n```diff\n{diff_content}\n```")
    else:
        result.append(SUB_AGENT_SECTIONS["mode"])

    return "\n\n".join(result)


# Admin-mode sections to suppress in swarm admin prompt
_ADMIN_SUPPRESS_SECTIONS: set[str] = set()

# Worker-mode sections to suppress in swarm worker prompt
_WORKER_SUPPRESS_SECTIONS: set[str] = set()


def _admin_sections() -> list[tuple[str, callable]]:
    """Return main sections for swarm admin prompt with applicable sections suppressed."""
    all_sections = _main_sections()
    return [
        (key, fn) for key, fn in all_sections
        if key not in _ADMIN_SUPPRESS_SECTIONS
    ]


def _worker_sections() -> list[tuple[str, callable]]:
    """Return main sections for swarm worker prompt with applicable sections suppressed."""
    all_sections = _main_sections()
    return [
        (key, fn) for key, fn in all_sections
        if key not in _WORKER_SUPPRESS_SECTIONS
    ]


def build_swarm_admin_prompt(active_skills_section: str = "") -> str:
    """Build system prompt for swarm admin (orchestrator mode).

    Reuses main sections but suppresses editing/task-list/temp-folder
    guidance. Appends swarm_admin_mode.md as the final mode section.

    Args:
        active_skills_section: Optional rendered active-skills block.

    Returns:
        Complete system prompt string
    """
    result = _build_prompt(_admin_sections())

    # Append the swarm admin mode section as the final section
    mode_content = _static("swarm_admin_mode.md")
    if mode_content:
        result += "\n\n" + mode_content

    # Append activity label guidance for dispatch_swarm_task
    result += (

        "\n\n### Activity labels\n"
        "- **Generate a concise `activity_label` (3-6 words) for every task you dispatch.** "
        "Think of it as a toolbar headline that tells the user what the worker is doing right now.\n"
        "- When calling `dispatch_swarm_task`, always provide an `activity_label` — a short 3-6 word "
        "phrase describing what the worker will do. This is displayed in the toolbar so the user can "
        "see what each worker is working on at a glance. "
        'Examples: `"fixing login redirect bug"`, `"adding unit tests for auth"`, '
        '`"refactoring database layer"`.'
    )

    if active_skills_section.strip():
        result += "\n\n" + active_skills_section.strip()

    return result


def build_swarm_worker_prompt() -> str:
    """Build system prompt for swarm worker.

    Reuses main sections with mode and sub-agent guidance suppressed
    (workers are leaf agents). Appends swarm_worker_mode.md as the
    final mode section.

    Returns:
        Complete system prompt string
    """
    result = _build_prompt(_worker_sections())

    # Append the swarm worker mode section as the final section
    mode_content = _static("swarm_worker_mode.md")
    if mode_content:
        result += "\n\n" + mode_content

    return result


