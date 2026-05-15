"""User skill storage and active session skill helpers."""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Generic, TypeVar

import yaml


logger = logging.getLogger(__name__)

MAX_SKILL_BYTES = 32 * 1024
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class SkillSummary:
    name: str
    path: Path
    preview: str
    modified: float
    description: str = ""
    tags: list[str] = field(default_factory=list)


T = TypeVar("T")


@dataclass
class SearchCandidate(Generic[T]):
    item: T
    text: str
    compact_text: str
    exact_text: str = ""


@dataclass
class SearchMatch(Generic[T]):
    item: T
    score: float


class SkillError(ValueError):
    """Raised when a skill operation cannot be completed."""


def get_skills_dir() -> Path:
    """Return the configured skills directory."""
    override = os.environ.get("BONE_SKILLS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".bone" / "skills"


def ensure_skills_dir() -> Path:
    """Create and return the skills directory."""
    path = get_skills_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_skill_name(raw: str) -> str:
    """Normalize a user-provided skill name for filesystem storage."""
    return (raw or "").strip().lower().replace(" ", "_")


def validate_skill_name(raw: str) -> str:
    """Validate and return a normalized skill name."""
    name = normalize_skill_name(raw)
    if not SKILL_NAME_RE.fullmatch(name):
        raise SkillError(
            "Invalid skill name. Use lowercase letters, numbers, underscores, "
            "or hyphens; start with a letter or number."
        )
    if "/" in name or "\\" in name or name.startswith(".") or ".." in name:
        raise SkillError("Invalid skill name.")
    return name


def get_skill_path(name: str) -> Path:
    """Return the safe path for a skill name."""
    valid_name = validate_skill_name(name)
    base = ensure_skills_dir().resolve()
    return base / f"{valid_name}.md"


def _check_size(content: str) -> None:
    if len(content.encode("utf-8")) > MAX_SKILL_BYTES:
        raise SkillError(f"Skill is too large. Maximum size is {MAX_SKILL_BYTES} bytes.")


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Extract YAML frontmatter and remaining body from content.

    Returns:
        (metadata_dict, body_text). metadata_dict may be empty.

    Notes:
        If a frontmatter block is present but invalid, preserve the original content
        as body so callers do not silently discard user-authored metadata.
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}, content
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}, content
    if not isinstance(meta, dict):
        return {}, content
    body = content[match.end():]
    return meta, body


def _normalize_description(value: object) -> str:
    text = str(value or "").strip()
    return text


def _normalize_tags(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, (list, tuple, set)):
        candidates = list(value)
    else:
        candidates = [value]

    tags: list[str] = []
    for candidate in candidates:
        tag = str(candidate or "").strip()
        if tag:
            tags.append(tag)
    return tags


def _render_frontmatter(description: str, tags: list[str]) -> str:
    """Render YAML frontmatter block for a skill file."""
    if not description and not tags:
        return ""
    meta = {}
    if description:
        meta["description"] = description
    if tags:
        meta["tags"] = tags
    return f"---\n{yaml.dump(meta, default_flow_style=False).strip()}\n---\n"


def _needs_metadata(meta: dict) -> bool:
    """Check if frontmatter is missing description or tags."""
    return not meta.get("description") or not meta.get("tags")


def _strip_heading(name: str, content: str) -> str:
    lines = content.splitlines()
    if not lines:
        return ""
    match = _HEADING_RE.match(lines[0])
    if match and normalize_skill_name(match.group(1)) == normalize_skill_name(name):
        return "\n".join(lines[1:]).strip()
    return content.strip()


def format_skill_file(name: str, content: str, *, description: str = "", tags: list[str] | None = None) -> str:
    """Format a skill as a markdown file with optional frontmatter and title heading."""
    valid_name = validate_skill_name(name)
    body = _strip_heading(valid_name, content)
    if not body:
        raise SkillError("Skill prompt cannot be empty.")

    frontmatter = _render_frontmatter(description, tags or [])
    formatted = f"{frontmatter}# {valid_name}\n\n{body.strip()}\n"
    _check_size(formatted)
    return formatted


def read_skill(name: str, strip_heading: bool = True) -> str:
    """Read a skill body by name.

    Returns the prompt body without frontmatter or heading (unless strip_heading=False,
    in which case frontmatter is still stripped but heading is kept).
    """
    path = get_skill_path(name)
    if path.is_symlink():
        raise SkillError("Refusing to read a symlinked skill.")
    if not path.is_file():
        raise SkillError(f"Skill '{validate_skill_name(name)}' not found.")
    content = path.read_text(encoding="utf-8")
    _, body = _parse_frontmatter(content)
    if strip_heading:
        return _strip_heading(name, body)
    return body.strip()


def write_skill(name: str, content: str, overwrite: bool = False) -> Path:
    """Create or replace a skill file.

    If the content contains YAML frontmatter with description and tags, those are
    preserved. Otherwise, metadata is auto-generated from the content via the LLM.
    """
    valid_name = validate_skill_name(name)
    path = get_skill_path(valid_name)
    if path.exists() and not overwrite:
        raise SkillError(f"Skill '{valid_name}' already exists.")

    # Parse any existing frontmatter from the content
    body = content
    description = ""
    tags: list[str] = []

    # Check if the raw content has frontmatter already
    raw_meta, raw_body = _parse_frontmatter(content)
    if raw_meta:
        description = _normalize_description(raw_meta.get("description", ""))
        tags = _normalize_tags(raw_meta.get("tags"))
        body = raw_body

    # If still missing metadata, try to preserve from existing file
    if _needs_metadata({"description": description, "tags": tags}) and path.is_file():
        existing_content = path.read_text(encoding="utf-8")
        existing_meta, _ = _parse_frontmatter(existing_content)
        if not description and existing_meta.get("description"):
            description = _normalize_description(existing_meta["description"])
        if not tags and existing_meta.get("tags"):
            tags = _normalize_tags(existing_meta.get("tags"))

    # If still missing, auto-generate a basic description
    if _needs_metadata({"description": description, "tags": tags}):
        prompt_body = _strip_heading(valid_name, body)
        if prompt_body and not description:
            description = prompt_body.strip()[:200]
        if not tags:
            tags = ["skill"]

    formatted = format_skill_file(valid_name, body, description=description, tags=tags)
    _atomic_write(path, formatted)
    return path


def remove_skill(name: str) -> Path:
    """Remove a skill file."""
    path = get_skill_path(name)
    if not path.is_file():
        raise SkillError(f"Skill '{validate_skill_name(name)}' not found.")
    if path.is_symlink():
        raise SkillError("Refusing to remove a symlinked skill.")
    path.unlink()
    return path


def list_skills(query: str | None = None) -> list[SkillSummary]:
    """List stored skills, optionally filtering by name/body preview."""
    return [match.item for match in search_skill_matches(query=query)]


def iter_skill_summaries() -> list[SkillSummary]:
    """Return all valid stored skill summaries."""
    base = ensure_skills_dir()
    summaries: list[SkillSummary] = []

    for path in sorted(base.glob("*.md")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            name = validate_skill_name(path.stem)
            raw = path.read_text(encoding="utf-8")
            meta, body_text = _parse_frontmatter(raw)
            heading_stripped = _strip_heading(name, body_text)
        except SkillError:
            continue

        summaries.append(
            SkillSummary(
                name=name,
                path=path,
                preview=_preview(heading_stripped),
                modified=path.stat().st_mtime,
                description=_normalize_description(meta.get("description", "")),
                tags=_normalize_tags(meta.get("tags")),
            )
        )
    return summaries


def search_candidates(
    query: str,
    candidates: list[SearchCandidate[T]],
    *,
    max_results: int = 5,
    item_key: Callable[[T], str] | None = None,
) -> list[SearchMatch[T]]:
    """Score and return matching candidates in descending relevance order."""
    query_text = (query or "").strip().lower()
    if not query_text:
        matches = [SearchMatch(item=candidate.item, score=0.0) for candidate in candidates]
        if item_key is not None:
            matches.sort(key=lambda match: item_key(match.item))
        return matches[:max_results]

    query_compact = _compact_match_text(query_text)
    query_terms = [term for term in query_text.split() if term]
    scored: list[SearchMatch[T]] = []

    for candidate in candidates:
        text = candidate.text.lower()
        compact_text = candidate.compact_text or _compact_match_text(text)
        exact_text = (candidate.exact_text or "").lower()
        score = 0.0

        if exact_text and query_text == exact_text:
            score += 120.0
        if exact_text and query_text in exact_text:
            score += 60.0
        if query_text in text:
            score += 40.0
        if query_compact and query_compact in compact_text:
            score += 25.0

        for term in query_terms:
            if exact_text and term in exact_text:
                score += 15.0
            if term in text:
                score += 10.0

        if score > 0:
            scored.append(SearchMatch(item=candidate.item, score=score))

    scored.sort(
        key=lambda match: (
            -match.score,
            item_key(match.item) if item_key is not None else "",
        )
    )
    return scored[:max_results]


def search_skill_matches(query: str | None = None, max_results: int = 20) -> list[SearchMatch[SkillSummary]]:
    """Return scored skill matches for discovery surfaces."""
    skills = iter_skill_summaries()
    candidates = [
        SearchCandidate(
            item=skill,
            text=" ".join(
                part
                for part in [
                    skill.name,
                    skill.description,
                    skill.preview,
                    " ".join(skill.tags),
                ]
                if part
            ),
            compact_text=_compact_match_text(
                " ".join(
                    part
                    for part in [skill.name, skill.description, " ".join(skill.tags)]
                    if part
                )
            ),
            exact_text=skill.name,
        )
        for skill in skills
    ]
    return search_candidates(
        query or "",
        candidates,
        max_results=max_results,
        item_key=lambda skill: skill.name,
    )


def activate_skill(chat_manager, name: str, content: str | None = None, reload: bool = False) -> int:
    """Activate a skill in session state and refresh the system prompt."""
    valid_name = validate_skill_name(name)
    body = (content if content is not None else read_skill(valid_name)).strip()
    if not body:
        raise SkillError("Skill prompt cannot be empty.")

    loaded_skills = getattr(chat_manager, "loaded_skills", None)
    if loaded_skills is None:
        loaded_skills = set()
        setattr(chat_manager, "loaded_skills", loaded_skills)
    if valid_name in loaded_skills and not reload:
        raise SkillError(f"Skill '{valid_name}' is already active in this chat.")

    loaded_skills.add(valid_name)
    if hasattr(chat_manager, "update_system_prompt"):
        chat_manager.update_system_prompt()
    else:
        chat_manager._update_context_tokens()

    return chat_manager.token_tracker.estimate_tokens(render_active_skills_section([valid_name]))


def get_active_skill_contents(skill_names: list[str] | set[str] | tuple[str, ...]) -> list[tuple[str, str]]:
    """Return validated active skill name/body pairs sorted by skill name."""
    active_skills = []
    for raw_name in sorted({validate_skill_name(name) for name in skill_names}):
        body = read_skill(raw_name)
        if body:
            active_skills.append((raw_name, body))
    return active_skills


def render_active_skills_section(skill_names: list[str] | set[str] | tuple[str, ...]) -> str:
    """Render active skills for inclusion in the system prompt."""
    try:
        active_skills = get_active_skill_contents(skill_names)
    except SkillError:
        active_skills = []
    if not active_skills:
        return ""

    sections = ["## Active skills", "Apply these active skill instructions in addition to the base prompt."]
    for name, body in active_skills:
        sections.append(f"### {name}\n{body}")
    return "\n\n".join(sections)


def _preview(content: str, max_chars: int = 90) -> str:
    text = " ".join(content.split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _compact_match_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.stem}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        tmp_path.replace(path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
