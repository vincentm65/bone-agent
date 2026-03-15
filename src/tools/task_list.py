"""Task list management tools.

These tools provide in-session task tracking for long EDIT workflows.
"""

from pathlib import Path
from typing import Optional, List

from .helpers.base import tool
from .helpers.converters import coerce_int
from . import constants


def _format_task_list(task_list, title=None):
    """Format task list for display.

    Args:
        task_list: List of task dicts with 'description' and 'completed' keys
        title: Optional title for the task list

    Returns:
        Formatted task list string
    """
    if not task_list:
        return "exit_code=1\nerror: No task list exists. Use create_task_list first.\n\n"

    safe_title = (title or "").strip() if isinstance(title, str) else ""
    safe_title = safe_title[:constants.MAX_TASK_TITLE_LEN] if safe_title else "untitled"

    done_count = 0
    lines = [f"Task list: {safe_title} (done={done_count} total={len(task_list)})"]

    for i, task in enumerate(task_list):
        is_done = bool(task.get("completed"))
        if is_done:
            done_count += 1
        checkbox = "[x]" if is_done else "[ ]"
        desc = str(task.get("description", ""))
        if len(desc) > constants.MAX_TASK_LEN:
            desc = desc[:constants.MAX_TASK_LEN - 3] + "..."
        lines.append(f"{i}: {checkbox} {desc}")

    # Update header with final done_count
    lines[0] = f"Task list: {safe_title} (done={done_count} total={len(task_list)})"
    return "\n".join(lines) + "\n\n"


@tool(
    name="create_task_list",
    description="Create or replace an in-session task list for tracking long EDIT workflows.",
    parameters={
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Task descriptions. Non-empty after trimming."
            },
            "title": {
                "type": "string",
                "description": "Optional short title for the task list."
            }
        },
        "required": ["tasks"]
    },
    allowed_modes=["edit"],
    requires_approval=False
)
def create_task_list(
    tasks: List[str],
    chat_manager,
    title: Optional[str] = None,
) -> str:
    """Create or replace an in-session task list.

    Args:
        tasks: List of task descriptions
        chat_manager: ChatManager instance (injected by context)
        title: Optional title for the task list

    Returns:
        Formatted task list result
    """
    # Validate interaction mode
    interaction_mode = getattr(chat_manager, 'interaction_mode', 'edit')
    if interaction_mode == "plan":
        return "exit_code=1\nerror: Task lists are disabled in PLAN mode. Switch to EDIT mode.\n\n"

    # Validate title
    if title is not None and not isinstance(title, str):
        return "exit_code=1\nerror: 'title' must be a string.\n\n"
    title = title.strip() if isinstance(title, str) else None
    if title:
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
    description="Mark one or more tasks complete in the current in-session task list.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "integer",
                "description": "Zero-based index of a single task to mark complete."
            },
            "task_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Array of zero-based task indices to mark complete."
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
    description="Show the current in-session task list without modifying it.",
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
