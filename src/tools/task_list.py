"""Task list management tools.

These tools provide in-session task tracking for long EDIT workflows.
"""

import textwrap
from pathlib import Path
from typing import Optional, List

from .helpers.base import tool
from .helpers.converters import coerce_int
from . import constants


def _escape_rich(text):
    """Escape square brackets in text so Rich renders them literally."""
    return text.replace("[", "\\[").replace("]", "\\]")
def _strip_rich_markup(text):
    """Remove Rich console markup tags from text for plain-text comparison.

    Handles [tag]...[/tag], [/tag], and standalone [tag] forms.
    Also un-escapes literal bracket sequences (\\[ \\]).
    """
    import re
    # Un-escape literal brackets first
    text = text.replace("\\[", "[").replace("\\]", "]")
    # Remove [/tag] closing tags
    text = re.sub(r'\[/\w+\]', '', text)
    # Remove [tag] opening tags (but not [x], [ ], or [N] patterns)
    text = re.sub(r'\[(?!x\]|\s?\]|\d+\])/?\w+\]', '', text)
    return text


def _format_task_list(task_list, title=None):
    """Format task list for display with Rich markup.

    Args:
        task_list: List of task dicts with 'description' and 'completed' keys
        title: Optional title for the task list

    Returns:
        Formatted task list string with Rich markup
    """
    if not task_list:
        return "exit_code=1\nerror: No task list exists. Use create_task_list first.\n\n"

    safe_title = (title or "").strip() if isinstance(title, str) else ""
    safe_title = safe_title[:constants.MAX_TASK_TITLE_LEN] if safe_title else "untitled"

    done_count = sum(1 for t in task_list if t.get("completed"))
    total = len(task_list)
    all_done = done_count == total

    # Escape user-provided text to prevent Rich markup injection
    escaped_title = _escape_rich(safe_title)

    # Header with progress
    if all_done:
        header = f"[bold green]\u2713[/bold green] [bold]{escaped_title}[/bold] [green]({done_count}/{total} done)[/green]"
    else:
        header = f"[bold]{escaped_title}[/bold] [dim]({done_count}/{total} done)[/dim]"

    lines = [header]

    # Indent for task lines: 2 spaces + bullet + space = 4 visible chars before description
    TASK_INDENT = "  "
    BULLET_DONE = "[dim green]\u2713[/dim green]"
    BULLET_PENDING = "[dim white]\u25cb[/dim white]"
    # Visible width of bullet prefix: "  ✓ " = 4 chars
    # Continuation indent must match for alignment
    DESC_INDENT = "    "  # 4 spaces, aligns with description after bullet
    TASK_WRAP_WIDTH = 60  # Reasonable width for wrapped task descriptions

    for i, task in enumerate(task_list):
        is_done = bool(task.get("completed"))
        desc = str(task.get("description", ""))
        if len(desc) > constants.MAX_TASK_LEN:
            desc = desc[:constants.MAX_TASK_LEN - 3] + "..."
        escaped_desc = _escape_rich(desc)

        if is_done:
            bullet = BULLET_DONE
            # Wrap description text only (no bullet), apply markup separately
            desc_lines = textwrap.wrap(escaped_desc, width=TASK_WRAP_WIDTH - 4, break_long_words=True, break_on_hyphens=True)
            if not desc_lines:
                desc_lines = [escaped_desc]
            # First line: bullet + struck-through description
            first_line = f"{TASK_INDENT}{bullet} [dim strike]{desc_lines[0]}[/dim strike]"
            lines.append(first_line)
            # Continuation lines: indent + struck-through text (no bullet)
            for dline in desc_lines[1:]:
                lines.append(f"{DESC_INDENT}[dim strike]{dline}[/dim strike]")
        else:
            bullet = BULLET_PENDING
            # Wrap long pending descriptions with proper indentation
            desc_lines = textwrap.wrap(escaped_desc, width=TASK_WRAP_WIDTH - 4, break_long_words=True, break_on_hyphens=True)
            if not desc_lines:
                desc_lines = [escaped_desc]
            first_line = f"{TASK_INDENT}{bullet} {desc_lines[0]}"
            lines.append(first_line)
            for dline in desc_lines[1:]:
                lines.append(f"{DESC_INDENT}{dline}")

    return "\n".join(lines) + "\n\n"


@tool(
    name="create_task_list",
    description="Create or replace an in-session task list for tracking long edit workflows.",
    parameters={
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Task descriptions (non-empty after trimming)"
            },
            "title": {
                "type": "string",
                "description": "Short title summarizing the workflow (e.g. 'Add pagination to user API'). Always provide a meaningful title."
            }
        },
        "required": ["tasks", "title"]
    },
    allowed_modes=["edit"],
    requires_approval=False
)
def create_task_list(
    tasks: List[str],
    chat_manager,
    title: str,
) -> str:
    """Create or replace an in-session task list.

    Args:
        tasks: List of task descriptions
        chat_manager: ChatManager instance (injected by context)
        title: Title for the task list

    Returns:
        Formatted task list result
    """
    # Validate interaction mode
    interaction_mode = getattr(chat_manager, 'interaction_mode', 'edit')
    if interaction_mode == "plan":
        return "exit_code=1\nerror: Task lists are disabled in PLAN mode. Switch to EDIT mode.\n\n"

    # Validate title
    if not isinstance(title, str):
        return "exit_code=1\nerror: 'title' must be a string.\n\n"
    title = title.strip()
    if not title:
        return "exit_code=1\nerror: 'title' must be non-empty.\n\n"
    title = title[:constants.MAX_TASK_TITLE_LEN]

    # Normalize tasks
    normalized = []
    for i, task in enumerate(tasks):
        if not isinstance(task, str):
            return f"exit_code=1\nerror: Task at index {i} must be a string.\n\n"
        trimmed = task.strip()
        if not trimmed:
            return f"exit_code=1\nerror: Task at index {i} must be non-empty.\n\n"
        if len(trimmed) > constants.MAX_TASK_LEN:
            return (
                f"exit_code=1\nerror: Task at index {i} exceeds MAX_TASK_LEN={constants.MAX_TASK_LEN}.\n\n"
            )
        normalized.append(trimmed)

    if len(normalized) == 0:
        return "exit_code=1\nerror: Provide at least one non-empty task.\n\n"
    if len(normalized) > constants.MAX_TASKS:
        return f"exit_code=1\nerror: Too many tasks (max {constants.MAX_TASKS}).\n\n"

    # Set task list on chat_manager
    chat_manager.task_list = [
        {"description": t, "completed": False}
        for t in normalized
    ]
    chat_manager.task_list_title = title or None

    return _format_task_list(chat_manager.task_list, chat_manager.task_list_title)


@tool(
    name="complete_task",
    description="Mark one or more tasks complete in the current task list.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "integer",
                "description": "Zero-based index of a single task to complete"
            },
            "task_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Zero-based task indices to complete"
            }
        }
    },
    allowed_modes=["edit"],
    requires_approval=False
)
def complete_task(
    chat_manager,
    task_id: Optional[int] = None,
    task_ids: Optional[List[int]] = None
) -> str:
    """Mark one or more tasks as complete.

    Args:
        chat_manager: ChatManager instance (injected by context)
        task_id: Single task index to mark complete
        task_ids: Multiple task indices to mark complete

    Returns:
        Formatted task list result
    """
    # Validate interaction mode
    interaction_mode = getattr(chat_manager, 'interaction_mode', 'edit')
    if interaction_mode == "plan":
        return "exit_code=1\nerror: Task lists are disabled in PLAN mode. Switch to EDIT mode.\n\n"

    # Normalize to list: prefer task_ids if both provided
    if task_ids is not None:
        ids_raw = task_ids
    elif task_id is not None:
        ids_raw = [task_id]
    else:
        return "exit_code=1\nerror: Either 'task_id' or 'task_ids' must be provided.\n\n"

    if not isinstance(ids_raw, list):
        return "exit_code=1\nerror: IDs must be an array of integers.\n\n"

    task_list = getattr(chat_manager, "task_list", None) or []
    if not task_list:
        return "exit_code=1\nerror: No task list exists. Use create_task_list first.\n\n"

    # Validate all IDs
    valid_ids = []
    for i, tid in enumerate(ids_raw):
        tid_int, error = coerce_int(tid)
        if error:
            return f"exit_code=1\nerror: ID at index {i}: {error}\n\n"
        if tid_int < 0:
            return f"exit_code=1\nerror: ID at index {i} must be non-negative.\n\n"
        if tid_int >= len(task_list):
            return (
                f"exit_code=1\nerror: ID {tid_int} (index {i}) is out of range (0-{len(task_list) - 1}).\n\n"
            )
        valid_ids.append(tid_int)

    # Mark tasks as complete
    for tid in valid_ids:
        task_list[tid]["completed"] = True

    return _format_task_list(task_list, chat_manager.task_list_title)


@tool(
    name="show_task_list",
    description="Show the current task list without modifying it.",
    parameters={"type": "object", "properties": {}},
    allowed_modes=["edit"],
    requires_approval=False
)
def show_task_list(
    chat_manager
) -> str:
    """Display the current task list.

    Args:
        chat_manager: ChatManager instance (injected by context)

    Returns:
        Formatted task list result
    """
    # Validate interaction mode
    interaction_mode = getattr(chat_manager, 'interaction_mode', 'edit')
    if interaction_mode == "plan":
        return "exit_code=1\nerror: Task lists are disabled in PLAN mode. Switch to EDIT mode.\n\n"

    task_list = getattr(chat_manager, "task_list", None) or []
    title = getattr(chat_manager, "task_list_title", None)

    return _format_task_list(task_list, title)
