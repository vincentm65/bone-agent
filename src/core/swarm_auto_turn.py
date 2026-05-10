"""Shared helpers for converting swarm server inbox items into auto-turn prompts.

Used by both the main loop (raw_input==130 path) and the background inbox
poller thread, plus the agentic orchestrator for mid-turn injection.
"""

import re


def _build_auto_turn_item(extra: dict) -> str:
    """Build a concise synthetic prompt for one pending swarm work item.

    Args:
        extra: Dict in the format returned by
               ``_inbox_to_auto_turn_extra()``.

    Returns:
        A short directive string for the admin LLM.
    """
    kind = extra.get("kind", "")
    task_id = extra.get("task_id", "-")
    worker_id = extra.get("worker_id", "-")
    display_name = extra.get("display_name", "")
    _m = re.match(r'worker-(\d+)', worker_id)
    worker_label = display_name if display_name else (
        f"Worker {int(_m.group(1))}" if _m else worker_id
    )

    # ── Command approval ──────────────────────────────────────────────
    if extra.get("status") == "approval_pending":
        call_id = extra.get("call_id", "-")
        command = str(extra.get("command_preview", "")).strip()
        reason = str(extra.get("reason", "")).strip()
        parts = [
            "[AUTO-TURN] Worker needs command approval.",
            f"Task {task_id}  ·  Call {call_id}  ·  Worker {worker_label}",
        ]
        if command:
            parts.append(f"Command: {command}")
        if reason:
            parts.append(f"Reason: {reason}")
        parts.extend([
            "",
            "Call handle_approval() with the task_id and call_id above.",
        ])
        return "\n".join(parts)

    # ── Task completion ───────────────────────────────────────────────
    if kind in ("action_required_completion", "auto_continue_completion"):
        status = extra.get("status", "completed")
        summary = str(extra.get("summary", "")).strip()
        files = extra.get("files", "")
        task_type = extra.get("task_type", "implementation")
        is_research = task_type == "research"

        if kind == "action_required_completion" or status == "failed":
            if is_research:
                parts = ["[AUTO-TURN] Research task FAILED — action required."]
            else:
                parts = ["[AUTO-TURN] Task FAILED — action required."]
        else:
            if is_research:
                parts = ["[AUTO-TURN] Research completed."]
            else:
                parts = ["[AUTO-TURN] Task completed."]
        parts.append(f"Task {task_id}  ·  Worker {worker_label}")
        if status and status != "completed":
            parts.append(f"Status: {status}")
        if summary:
            parts.append(f"Summary: {summary}")
        if files:
            parts.append(f"Files: {files}")

        if is_research:
            parts.extend([
                "",
                "1. Review the research findings above.",
                "2. Dispatch more research if coverage gaps remain.",
                "3. When all research is complete, create the implementation task list and dispatch implementation tasks.",
            ])
        else:
            parts.extend([
                "",
                "1. Call complete_task() if the result is acceptable.",
                "2. Dispatch a revision task if changes are needed.",
                "3. Dispatch the next incomplete task from the task list.",
            ])
        return "\n".join(parts)

    # ── Fallback ──────────────────────────────────────────────────────
    text = extra.get("text", str(extra.get("summary", ""))).strip()
    return f"[AUTO-TURN]\n{text}" if text else "[AUTO-TURN] Swarm event."


def _inbox_to_auto_turn_extra(item: dict) -> dict | None:
    """Map a server inbox item to the format ``_build_auto_turn_item`` expects.

    Args:
        item: Raw inbox dict from ``SwarmServer.take_pending()``.

    Returns:
        A dict suitable for ``_build_auto_turn_item()``, or None if the
        item kind is unrecognized.
    """
    kind = item.get("kind", "")

    # ── Completion → auto_continue_completion ─────────────────────────
    if kind == "completion":
        status = item.get("status", "")
        task_type = item.get("task_type", "implementation")
        result_kind = "auto_continue_completion"
        if status == "failed":
            result_kind = "action_required_completion"
        return {
            "kind": result_kind,
            "task_id": item.get("task_id", "-"),
            "worker_id": item.get("worker_id", "-"),
            "display_name": item.get("display_name", ""),
            "status": status,
            "summary": item.get("summary", ""),
            "files": "",
            "task_type": task_type,
        }

    # ── Approval → approval_pending ───────────────────────────────────
    if kind == "approval_needed":
        return {
            "status": "approval_pending",
            "task_id": item.get("task_id", "-"),
            "call_id": item.get("call_id", "-"),
            "worker_id": item.get("worker_id", "-"),
            "display_name": item.get("display_name", ""),
            "command_preview": item.get("command_preview", ""),
            "reason": item.get("reason", ""),
        }

    # ── Unknown kind ──────────────────────────────────────────────────
    return None


def drain_inbox_to_prompts(server) -> list[str]:
    """Drain all pending items from the swarm server inbox and convert
    them to auto-turn prompt strings.

    Args:
        server: The SwarmServer instance (or None).

    Returns:
        List of synthetic prompt strings ready to inject as user messages.
    """
    if server is None:
        return []

    prompts = []
    while server.has_pending():
        item = server.take_pending()
        if item is None:
            break
        extra = _inbox_to_auto_turn_extra(item)
        if extra is not None:
            prompts.append(_build_auto_turn_item(extra))
    return prompts
