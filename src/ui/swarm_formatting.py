"""Shared swarm UX formatting helpers.

Worker labels (Worker 1 instead of worker-01), event-line formatting,
and the format_swarm_status snapshot formatter used by
/swarm status (commands.py).
"""

import re
from typing import Any

_WORKER_PATTERN = re.compile(r"^worker-(\d+)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Worker label
# ---------------------------------------------------------------------------

def format_worker_label(worker_id: str) -> str:
    """Return a user-friendly label, e.g. 'worker-01' -> 'Worker 1'."""
    m = _WORKER_PATTERN.match(worker_id)
    if m:
        return f"Worker {int(m.group(1))}"
    return worker_id


# ---------------------------------------------------------------------------
# Event-line helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Approval rendering helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Status snapshot formatter
# ---------------------------------------------------------------------------

def format_swarm_toolbar_lines(
    snapshot: dict,
    max_requests: int = 3,
    pending_by_worker: dict[str, list[dict[str, Any]]] | None = None,
) -> list[str]:
    """Return compact toolbar lines for live swarm status.

    Each line is plain text (no markup) suitable for prompt-toolkit's
    bottom toolbar.  Returns an empty list when there are no workers.

    Worker lines use a compact format:
      idle:           ``Worker 1 - idle``
      running:        ``Worker 2 - working`` or ``Worker 2 - working - task-a12``
      blocked+approval: ``Worker 3 - pending approval``
      blocked no approval: ``Worker 3 - blocked`` or ``Worker 3 - blocked - task-c56``
      unknown/other:  ``Worker N - {status}`` plus `` - {task_id}`` if present

    Pending approval is detected by checking ``approval_requests`` for a
    matching ``worker_id``.  Prompt previews are intentionally excluded
    from toolbar worker lines to keep them compact.

    Both ``max_requests`` and ``pending_by_worker`` are retained for
    backward compatibility but no longer used — all state is derived
    from the server snapshot.

    Args:
        snapshot: dict from ``SwarmServer.status_snapshot()``.
        max_requests: deprecated, ignored.
        pending_by_worker: deprecated, ignored.

    Returns:
        List of short status lines.
    """
    workers = snapshot.get("workers", {})
    if not workers:
        return ["Swarm: no workers"]

    approval_requests = snapshot.get("approval_requests", [])
    pending_approval_worker_ids: set[str] = {
        req.get("worker_id", "") for req in approval_requests
    }

    lines: list[str] = []

    # Worker lines — compact, one line per worker, no prompt previews.
    for wid, winfo in workers.items():
        label = format_worker_label(wid)
        status = winfo.get("status", "unknown")
        task_id = winfo.get("current_task_id")

        if status == "idle":
            lines.append(f"{label} - idle")
        elif status == "running":
            if task_id:
                lines.append(f"{label} - working - {task_id}")
            else:
                lines.append(f"{label} - working")
        elif status == "blocked":
            if wid in pending_approval_worker_ids:
                lines.append(f"{label} - pending approval")
            elif task_id:
                lines.append(f"{label} - blocked - {task_id}")
            else:
                lines.append(f"{label} - blocked")
        else:
            # Unknown or other status — include task id if present.
            base = f"{label} - {status}"
            if task_id:
                lines.append(f"{base} - {task_id}")
            else:
                lines.append(base)

    return lines


# ---------------------------------------------------------------------------
# Task-list / plan progress toolbar line
# ---------------------------------------------------------------------------

def format_task_list_toolbar_line(
    task_list: list | None,
    snapshot: dict | None = None,
    title: str | None = None,
    max_next_len: int = 70,
    swarm_complete: bool = False,
    swarm_complete_summary: str = "",
    plan_map: dict | None = None,
    max_visible: int = 6,
) -> list[str]:
    """Return toolbar lines showing plan checklist with in-flight markers.

    Designed for the bottom toolbar during swarm admin mode.  Shows
    completion status per task using ✓/↻/○ markers and truncates when
    the list exceeds ``max_visible`` lines.

    Args:
        task_list: List of task dicts with 'description' and 'completed'
                   keys.  May be None or empty.
        snapshot: Server status snapshot dict (from
                  ``SwarmServer.status_snapshot()``).  Contains ``tasks``
                  keyed by task_id with ``status``, ``plan_index``, etc.
        title: Task list title shown in the header line.
        max_next_len: Max length of task descriptions before truncation.
        swarm_complete: If True, show a completion banner instead.
        swarm_complete_summary: Summary text for the completion banner.
        plan_map: Optional dict mapping task_id -> plan_index, sourced
                  from ``chat_manager._swarm_task_plan_map``.
        max_visible: Maximum task lines to show before truncation.

    Returns:
        A list of plain text lines suitable for prompt-toolkit toolbar.
        Empty list when there is nothing to show.
    """
    # When swarm is marked complete, show a single-line completion banner.
    if swarm_complete:
        summary = swarm_complete_summary.strip()
        if summary:
            truncated = summary[:max_next_len] + "..." if len(summary) > max_next_len else summary
            return [f"Swarm: complete - {truncated}"]
        return ["Swarm: complete"]

    # No task list at all — nothing to show.
    if not task_list:
        return []

    total = len(task_list)
    done_count = sum(1 for t in task_list if t.get("completed"))

    # ------------------------------------------------------------------
    # Determine which plan indices are currently in-flight.
    # ------------------------------------------------------------------
    plan_indices_in_flight: set[int] = set()

    if snapshot:
        for _tid, tinfo in snapshot.get("tasks", {}).items():
            pi = tinfo.get("plan_index")
            status = tinfo.get("status", "")
            if pi is not None and status in ("dispatched", "running"):
                plan_indices_in_flight.add(int(pi))

    # plan_map fallback: includes tasks that may have left the server's
    # active dict but whose plan_index was recorded at dispatch time.
    if plan_map and snapshot:
        for _tid, pi in plan_map.items():
            # Cross-reference with snapshot to confirm still active.
            tinfo = snapshot.get("tasks", {}).get(_tid)
            if tinfo and tinfo.get("status") in ("dispatched", "running"):
                plan_indices_in_flight.add(int(pi))

    # ------------------------------------------------------------------
    # Assign markers to each task.
    # ------------------------------------------------------------------
    markers: list[str] = []
    for i, t in enumerate(task_list):
        if t.get("completed"):
            markers.append("\u2713")       # ✓
        elif i in plan_indices_in_flight:
            markers.append("\u21bb")       # ↻
        else:
            markers.append("\u25cb")       # ○

    # ------------------------------------------------------------------
    # Truncation: decide which task indices are visible.
    # ------------------------------------------------------------------
    if total <= max_visible:
        visible_indices = list(range(total))
    else:
        # a) All in-flight tasks MUST be visible.
        in_flight_idx = {i for i, m in enumerate(markers) if m == "\u21bb"}
        visible_set = set(in_flight_idx)

        # b) Find the first pending (○) task after the last in-flight —
        #    this is the "next in queue" and must be visible.
        last_inflight = max(in_flight_idx) if in_flight_idx else -1
        next_pending = None
        for i in range(last_inflight + 1, total):
            if markers[i] == "\u25cb":
                next_pending = i
                break
        if next_pending is not None:
            visible_set.add(next_pending)

        # c) Fill remaining slots with other tasks (preserving order)
        #    up to max_visible.
        for i in range(total):
            if len(visible_set) >= max_visible:
                break
            visible_set.add(i)

        visible_indices = sorted(visible_set)

    hidden_count = total - len(visible_indices)

    # ------------------------------------------------------------------
    # Build output lines.
    # ------------------------------------------------------------------
    display_title = title or "untitled"
    lines: list[str] = [f"{display_title} ({done_count}/{total} done)"]

    for i in visible_indices:
        desc = str(task_list[i].get("description", ""))
        truncated = desc[:max_next_len] + "..." if len(desc) > max_next_len else desc
        lines.append(f"  {markers[i]} {truncated}")

    if hidden_count > 0:
        lines.append(f"  +{hidden_count} more")

    return lines


# ---------------------------------------------------------------------------
# Queue / Activity toolbar line (page 3)
# ---------------------------------------------------------------------------


def format_swarm_status(snapshot: dict, mode: str = "human") -> str:
    """Format a swarm server status snapshot as a human-readable string.

    Args:
        snapshot: dict returned by ``SwarmServer.status_snapshot()``.
        mode: ``"human"`` for the CLI panel — compact summary lines.

    Returns:
        A multi-line formatted string.
    """
    lines: list[str] = []
    header = snapshot.get("swarm_name", "unknown")

    worker_count = snapshot.get("worker_count", 0)
    idle = snapshot.get("idle_workers", 0)
    running = snapshot.get("running_tasks", 0)
    pending_tasks = snapshot.get("pending_tasks", [])
    approval_requests = snapshot.get("approval_requests", [])

    lines.append(f"Swarm: {header}  |  Workers: {worker_count} ({idle} idle, {running} running)")
    lines.append(f"Queue: {len(pending_tasks)} pending  |  Approvals: {len(approval_requests)} pending")

    # Workers
    workers = snapshot.get("workers", {})
    if workers:
        lines.append("")
        lines.append("Workers:")
        for wid, winfo in workers.items():
            status = winfo.get("status", "unknown")
            task_id = winfo.get("current_task_id") or "none"
            label = format_worker_label(wid)
            lines.append(f"  {label}: {status}, task={task_id}")

    # Tasks
    tasks = snapshot.get("tasks", {})
    if tasks:
        lines.append("")
        lines.append("Tasks:")
        for tid, tinfo in tasks.items():
            status = tinfo.get("status", "unknown")
            worker_id = tinfo.get("worker_id") or "unassigned"
            prompt = tinfo.get("prompt", "")
            prompt_preview = prompt[:200] + "..." if len(prompt) > 200 else prompt
            label = format_worker_label(worker_id)
            lines.append(f"  {tid}: {status}, agent={label}, prompt={prompt_preview}")

    # Pending tasks (queued)
    if pending_tasks:
        lines.append("")
        lines.append("Pending tasks:")
        for idx, task in enumerate(pending_tasks, start=1):
            task_id = task.get("task_id", "unknown")
            prompt = task.get("prompt", "")
            prompt_preview = prompt[:200] + "..." if len(prompt) > 200 else prompt
            lines.append(f"  #{idx} {task_id}: prompt={prompt_preview}")

    # Pending approvals
    if approval_requests:
        lines.append("")
        lines.append("Pending approvals:")
        for approval in approval_requests:
            task_id = approval.get("task_id", "unknown")
            call_id = approval.get("call_id", "unknown")
            worker_id = approval.get("worker_id", "unknown")
            command = approval.get("command", "")
            command_preview = command[:200] + "..." if len(command) > 200 else command
            task_prompt = approval.get("task_prompt", "")
            write_scope = approval.get("task_write_scope", [])
            task_status = approval.get("task_status", "unknown")
            worker_status = approval.get("worker_status", "unknown")

            w_label = format_worker_label(worker_id)

            if mode == "agent" or mode == "detailed":
                # Agent/detailed mode — full structured block for each approval
                prompt_preview = task_prompt[:200] + "..." if task_prompt and len(task_prompt) > 200 else (task_prompt or "<unknown>")
                scope_str = ", ".join(write_scope) if write_scope else "<unknown>"

                lines.append(f"  {task_id}/{call_id} from {w_label}:")
                lines.append(f"    task_id={task_id}, call_id={call_id}, worker_id={w_label}")
                lines.append(f"    command={command_preview}")
                lines.append(f"    task_prompt={prompt_preview}")
                lines.append(f"    write_scope={scope_str}")
                lines.append(f"    task_status={task_status}, worker_status={worker_status}")
            else:
                # Human mode — compact summary line
                command_short = command[:120] + "..." if len(command) > 120 else command
                lines.append(f"  {task_id}/{call_id} from {w_label}: {command_short}")
                lines.append(f"    Awaiting admin approval — the admin agent will handle this.")
                lines.append(f"    To override manually: ask admin to approve or deny {task_id}/{call_id}.")

    return "\n".join(lines)
