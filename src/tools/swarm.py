"""Swarm pool tools for admin agent task dispatch."""

from pathlib import Path
from typing import Any, List, Optional

from .helpers.base import tool

def _require_swarm_admin(chat_manager: Any) -> tuple[bool, str]:
    if not chat_manager:
        return False, "Cannot access swarm: no chat manager available."

    if not getattr(chat_manager, "swarm_admin_mode", False) or not getattr(chat_manager, "swarm_server", None):
        return False, (
            "Not in swarm admin mode. Start a swarm first with the 'start' command "
            "or '/swarm start <name>' in the admin terminal."
        )

    return True, ""


@tool(
    name="dispatch_swarm_task",
    description=(
        "Dispatch a task to workers in an active swarm pool. "
        "Only works when the admin agent is in swarm admin mode "
        "(after starting a swarm with the swarm protocol). "
        "The task will be queued or dispatched to an idle worker. "
        "Returns task_id, status, assigned agent (or queued state), write scope, and prompt sent."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The task prompt to send to a worker. "
                "Describe what the worker should do.",
            },
            "write_scope": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of file paths the worker is expected to edit or create. "
                "Used for write-scope validation on the worker side.",
            },
            "plan_index": {
                "type": "integer",
                "description": "Zero-based index into the task list plan that this dispatch corresponds to. Used for status bar tracking.",
            },
        },
        "required": ["prompt"],
    },
    tier="core",
    tags=["swarm", "pool", "admin", "dispatch"],
    category="swarm",
)
def dispatch_swarm_task(
    prompt: str,
    write_scope: Optional[List[str]] = None,
    plan_index: Optional[int] = None,
    chat_manager: Any = None,
    repo_root: Path = None,
) -> str:
    """Dispatch a task to swarm workers via the admin agent.

    Args:
        prompt: The task prompt to send to a worker.
        write_scope: Expected file paths the worker will edit.
        plan_index: Zero-based index into the task list plan. Used for status bar tracking.
        chat_manager: The current chat manager instance (injected by orchestrator).
        repo_root: Repository root path (injected by orchestrator).

    Returns:
        Task status message with task_id, status, and queue info.
    """
    ok, error = _require_swarm_admin(chat_manager)
    if not ok:
        return f"exit_code=1\n{error}"

    if write_scope is not None and not isinstance(write_scope, list):
        return "exit_code=1\nwrite_scope must be a list of file paths."

    if not prompt or not prompt.strip():
        return "exit_code=1\nprompt must not be empty."

    server = chat_manager.swarm_server
    try:
        result = server.submit_task(prompt, write_scope=write_scope or [], plan_index=plan_index)
    except Exception as e:
        return f"exit_code=1\nFailed to dispatch task: {e}"

    if plan_index is not None and result.get("task_id"):
        if not hasattr(chat_manager, "_swarm_task_plan_map"):
            chat_manager._swarm_task_plan_map = {}
        chat_manager._swarm_task_plan_map[result["task_id"]] = plan_index

    agent = result.get("worker_id") or "queued"
    conn_info = server.connection_info
    parts = [
        "exit_code=0",
        f"Task: {result['task_id']}",
        f"Status: {result['status']}",
        f"Agent: {agent}",
        "Write scope:",
        "",
        "Connection info for new workers:",
        f"  URL: {conn_info['url']}",
        f"  Auth token: {conn_info['auth_token']}",
    ]

    if write_scope:
        parts.extend(f"- {path}" for path in write_scope)
    else:
        parts.append("- none")

    if result.get("queue_position") is not None:
        parts.append(f"Queue position: {result['queue_position']}")

    return "\n".join(parts)


@tool(
    name="handle_approval",
    description=(
        "Approve or deny a pending worker command that is waiting for admin approval. "
        "Only works when the admin agent is in swarm admin mode. "
        "Use this when a worker has requested approval to execute a command. "
        "Set approved=True to approve, approved=False to deny (reason required for denial)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task ID associated with the pending approval.",
            },
            "call_id": {
                "type": "string",
                "description": "The command call ID to approve or deny.",
            },
            "approved": {
                "type": "boolean",
                "description": "True to approve the command, False to deny it.",
            },
            "reason": {
                "type": "string",
                "description": "Reason for the decision. Required for denials, optional for approvals.",
            },
        },
        "required": ["task_id", "call_id", "approved"],
    },
    tier="core",
    tags=["swarm", "pool", "admin", "approval"],
    category="swarm",
)
def handle_approval(
    task_id: str,
    call_id: str,
    approved: bool,
    reason: Optional[str] = None,
    chat_manager: Any = None,
) -> str:
    """Approve or deny a pending worker command via the admin agent."""
    ok, error = _require_swarm_admin(chat_manager)
    if not ok:
        return f"exit_code=1\n{error}"

    if not task_id or not task_id.strip():
        return "exit_code=1\ntask_id must not be empty."
    if not call_id or not call_id.strip():
        return "exit_code=1\ncall_id must not be empty."

    if not approved and not reason:
        return "exit_code=1\nDenial requires a reason."

    server = chat_manager.swarm_server

    if approved:
        try:
            success = server.approve(task_id, call_id, guidance=reason or "")
        except Exception as e:
            return f"exit_code=1\nFailed to approve command: {e}"
        if success:
            return f"exit_code=0\nApproved command {task_id}/{call_id}."
        else:
            return f"exit_code=1\nNo pending approval found for {task_id}/{call_id}."
    else:
        try:
            success = server.deny(task_id, call_id, reason=reason or "Denied by admin.")
        except Exception as e:
            return f"exit_code=1\nFailed to deny command: {e}"
        if success:
            return f"exit_code=0\nDenied command {task_id}/{call_id}: {reason}."
        else:
            return f"exit_code=1\nNo pending approval found for {task_id}/{call_id}."


@tool(
    name="kill_swarm_worker",
    description=(
        "Permanently kill and remove a worker from the swarm pool. "
        "Use when a worker is rogue, stuck, or needs to be forcefully stopped — "
        "the worker is removed immediately, pending approvals are cancelled, "
        "and its active task (if any) is marked killed. "
        "The worker cannot rejoin unless a new process is started manually. "
        "Only works when the admin agent is in swarm admin mode."
    ),
    parameters={
        "type": "object",
        "properties": {
            "worker_id": {
                "type": "string",
                "description": "The worker ID to permanently kill and remove from the pool.",
            },
        },
        "required": ["worker_id"],
    },
    tier="core",
    tags=["swarm", "pool", "admin", "kill"],
    category="swarm",
)
def kill_swarm_worker(
    worker_id: str,
    chat_manager: Any = None,
) -> str:
    """Permanently kill a swarm worker via the admin agent."""
    ok, error = _require_swarm_admin(chat_manager)
    if not ok:
        return f"exit_code=1\n{error}"

    if not worker_id or not worker_id.strip():
        return "exit_code=1\nworker_id must not be empty."

    server = chat_manager.swarm_server
    try:
        success = server.kill_worker(worker_id)
    except Exception as e:
        return f"exit_code=1\nFailed to kill worker: {e}"

    if success:
        return f"exit_code=0\nKilled worker {worker_id} — removed from pool, approvals cancelled, task marked killed."
    else:
        return f"exit_code=1\nWorker {worker_id} not found in the swarm pool."
