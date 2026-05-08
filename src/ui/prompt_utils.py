"""Shared prompt utilities for bone-agent CLI."""

import re
import shutil
import time

from prompt_toolkit import PromptSession
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from prompt_toolkit.formatted_text import HTML
from llm.config import get_provider_config, APPROVE_MODE_LABELS, STATUS_BAR_SETTINGS
from ui.toolbar_interactions import (
    dispatch_toolbar_key,
    get_active_interaction,
    render_active_interaction,
    render_pending_interaction,
)
from ui.status_state import ProgressState


def _toolbar_width() -> int:
    """Return a conservative visible width for bottom-toolbar lines."""
    try:
        return max(20, shutil.get_terminal_size(fallback=(80, 24)).columns - 1)
    except Exception:
        return 79


def _escape_html(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _truncate_plain(text: str, width: int | None = None) -> str:
    """Truncate plain toolbar text so PTK never wraps it into artifacts."""
    width = width or _toolbar_width()
    text = str(text).replace("\r", " ").replace("\n", " ")
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1].rstrip() + "…"


def _style_line(text: str, fg: str = "#888888", width: int | None = None) -> str:
    return f'<style fg="{fg}">{_escape_html(_truncate_plain(text, width))}</style>'


def _separator_line(width: int) -> str:
    return _style_line("─" * width, "#777777", width)


def _style_task_toolbar_line(line: str, width: int | None = None) -> str:
    """Style task-list toolbar lines to match the Rich task-list display."""
    width = width or _toolbar_width()
    line = _truncate_plain(line, width)
    escaped = _escape_html(line)

    if line.startswith("  ✓ "):
        desc = _escape_html(_truncate_plain(line[4:], max(1, width - 4)))
        return f'  <style fg="green">✓</style> <s><style fg="#777777">{desc}</style></s>'
    if line.startswith("  ○ "):
        desc = _escape_html(_truncate_plain(line[4:], max(1, width - 4)))
        return f'  <style fg="#aaaaaa">○</style> <style fg="#aaaaaa">{desc}</style>'
    if line.startswith("  ↻ "):
        desc = _escape_html(_truncate_plain(line[4:], max(1, width - 4)))
        return f'  <style fg="cyan">↻</style> <style fg="#aaaaaa">{desc}</style>'
    if line.startswith("  +"):
        return f'<style fg="#888888">{escaped}</style>'
    match = re.match(r"^(.*?)(\s+\(\d+/\d+ done\))$", line)
    if match:
        title_text, progress_text = match.groups()
        return f'{_escape_html(title_text)}<style fg="#888888">{_escape_html(progress_text)}</style>'
    return escaped


def _build_status_line(chat_manager, approval_value: str, width: int) -> tuple[str, str]:
    """Build the model/tokens/cost status line shared by normal and worker toolbars.

    Returns (status_line: str, status_html: str) where status_line is the
    plain-text version and status_html is the styled HTML string.
    """
    provider_name = chat_manager.client.provider
    model = get_provider_config(provider_name).get("model", "Unknown")

    tokens_curr = chat_manager.token_tracker.current_context_tokens
    tokens_in = chat_manager.token_tracker.total_prompt_tokens
    tokens_out = chat_manager.token_tracker.total_completion_tokens
    tokens_total = chat_manager.token_tracker.total_tokens
    total_cost = chat_manager.token_tracker.get_display_cost(model)

    # Format model name (take last part if path)
    if "\\" in model or "/" in model:
        model_display = model.split("\\")[-1].split("/")[-1]
    else:
        model_display = model

    model_name = model_display or provider_name
    if model_name.endswith(".gguf"):
        model_name = model_name[:-5]

    status_parts = [model_name, f"Approval: {approval_value}"]
    optional_parts = []
    if STATUS_BAR_SETTINGS.get("show_curr_tokens", True):
        optional_parts.append(f"curr {tokens_curr:,}")
    if STATUS_BAR_SETTINGS.get("show_in_tokens", True):
        optional_parts.append(f"in {tokens_in:,}")
    if STATUS_BAR_SETTINGS.get("show_out_tokens", True):
        optional_parts.append(f"out {tokens_out:,}")
    if STATUS_BAR_SETTINGS.get("show_total_tokens", True):
        optional_parts.append(f"total {tokens_total:,}")
    if STATUS_BAR_SETTINGS.get("show_cost", True):
        optional_parts.append(f"${total_cost:.4f}")

    for part in optional_parts:
        candidate = " | ".join(status_parts + [part])
        if len(candidate) <= width:
            status_parts.append(part)

    status_line = " | ".join(status_parts)
    if len(status_line) > width:
        approval = f"Approval: {approval_value}"
        reserved = len(" | ") + len(approval)
        model_budget = max(8, width - reserved)
        status_line = f"{_truncate_plain(model_name, model_budget)} | {approval}"
    status_html = _style_line(status_line, "#606060", width)
    return status_line, status_html


def _join_toolbar_sections(*sections: str) -> str:
    """Join toolbar sections so each section starts on its own line."""
    return "\n".join(section.rstrip("\n") for section in sections if section)


def _join_toolbar_sections_with_gap(*sections: str) -> str:
    """Join toolbar sections with one blank line between each section."""
    return "\n\n".join(section.rstrip("\n") for section in sections if section)


def get_bottom_toolbar_text(chat_manager):
    """Return bottom toolbar text with model, approval mode, and token count.

    This is extracted from main.py for reuse in confirmation prompts.

    When an active toolbar interaction is present (e.g. select_option,
    tool approval), it takes priority and a compact status line is shown
    below it.

    Args:
        chat_manager: ChatManager instance for state access

    Returns:
        HTML formatted toolbar text
    """
    # Active interaction: status bar on top, separator, interaction content below.
    active_render = render_active_interaction(chat_manager)
    if active_render is not None:
        status = _get_normal_status_text(chat_manager, include_progress=False)
        sep = _separator_line(_toolbar_width())
        return HTML(_join_toolbar_sections(status, sep, active_render))

    # Pending interaction: status bar on top, separator, interaction prompt below.
    pending_render = render_pending_interaction(chat_manager)
    if pending_render is not None:
        status = _get_normal_status_text(chat_manager, include_progress=False)
        sep = _separator_line(_toolbar_width())
        return HTML(_join_toolbar_sections(status, sep, pending_render))

    # No interaction: normal status only.
    return HTML(_get_normal_status_text(chat_manager))


def get_worker_toolbar_text(chat_manager, worker_runner):
    """Return bottom toolbar text for swarm worker mode.

    Shows model status, worker identity/state, and progress indicators.
    Workers run in auto-approve mode and show their own identity line
    rather than the admin's swarm page switching.

    Args:
        chat_manager: ChatManager instance for state access
        worker_runner: WorkerRunner instance with worker metadata

    Returns:
        HTML formatted toolbar text
    """
    # Active interaction: status bar on top, separator, interaction content below.
    active_render = render_active_interaction(chat_manager)
    if active_render is not None:
        status = _get_worker_status_text(chat_manager, worker_runner, include_progress=False)
        sep = _separator_line(_toolbar_width())
        return HTML(_join_toolbar_sections(status, sep, active_render))

    # Pending interaction: status bar on top, separator, interaction prompt below.
    pending_render = render_pending_interaction(chat_manager)
    if pending_render is not None:
        status = _get_worker_status_text(chat_manager, worker_runner, include_progress=False)
        sep = _separator_line(_toolbar_width())
        return HTML(_join_toolbar_sections(status, sep, pending_render))

    # No interaction: normal worker status.
    return HTML(_get_worker_status_text(chat_manager, worker_runner, include_progress=True))


def _get_worker_status_text(chat_manager, worker_runner, include_progress: bool = True):
    """Return the full worker status toolbar text (model, tokens, cost, worker identity, progress).

    Returns a string suitable for concatenating and wrapping in ``HTML()``.
    """
    # Above-status progress: generic spinner + active tool
    progress_above = _get_progress_above_text(chat_manager) if include_progress else ""

    width = _toolbar_width()
    _status_line, status_html = _build_status_line(chat_manager, "auto", width)

    # Worker-specific identity line
    worker_id = _escape_html(str(getattr(worker_runner, 'worker_id', '?')))
    swarm_name = _escape_html(str(getattr(worker_runner, 'swarm_name', '')))
    client = getattr(worker_runner, '_client', None)
    is_connected = getattr(client, 'is_connected', False) if client else False
    conn = "connected" if is_connected else "disconnected"
    busy = getattr(worker_runner, '_busy', None)
    task_id = getattr(worker_runner, '_task_id', '')
    if busy and busy.is_set() and task_id:
        state_str = f"working on {_escape_html(str(task_id))}"
    elif busy and busy.is_set():
        state_str = "working"
    else:
        state_str = "idle"

    worker_line = f"Worker {worker_id} | {conn} | {state_str} | {swarm_name}"
    worker_html = _style_line(worker_line, "#aaaaaa", width)

    # Prepend progress_above (spinner / active tool) when present
    if progress_above:
        toolbar_text = progress_above.rstrip('\n') + '\n' + status_html + '\n' + worker_html
    else:
        toolbar_text = status_html + '\n' + worker_html

    # Below-status: subagent active / done
    progress_below = _get_progress_below_text(chat_manager) if include_progress else ""
    if progress_below:
        toolbar_text += '\n' + _separator_line(width) + '\n' + progress_below

    return toolbar_text


def _get_progress_above_text(chat_manager):
    """Return progress text for above the model status bar.

    Generic spinner (Thinking ...) and active tool indicator.
    Returns an empty string if neither is active.

    When a subagent is active, the generic spinner is suppressed to avoid
    duplicate status display (the subagent line in _get_progress_below_text
    handles all progress rendering).
    """
    progress = getattr(chat_manager, 'progress', None)
    if progress is None:
        return ""

    # Suppress generic spinner when subagent is active to avoid duplicate
    if progress.subagent_active:
        return ""

    # Spinner — shown when no subagent is active
    spinner_text = progress.get_spinner_text()
    if spinner_text:
        parts = spinner_text.split(" ", 1)
        frame = parts[0]
        msg = parts[1] if len(parts) > 1 else ""
        line = _truncate_plain(f"{frame} {msg}")
        frame_part, _, msg_part = line.partition(" ")
        return f'<style fg="cyan">{_escape_html(frame_part)}</style> <style fg="white">{_escape_html(msg_part)}</style>\n'

    # Active tool
    tool_name = progress.active_tool_name
    if tool_name:
        return _style_line(f"* {tool_name}", "#5F9EA0") + " "

    return ""


def _get_progress_below_text(chat_manager):
    """Return progress text for below the model status bar.

    Active subagent renders a header line (spinner + summary) followed by
    a bounded multi-line activity log (max 5 entries).  Done-state auto-
    dismisses after 3 seconds.
    Returns an empty string if neither is active.
    """
    progress = getattr(chat_manager, 'progress', None)
    if progress is None:
        return ""

    sa = progress.get_subagent_summary()

    # Subagent active — header line + bounded activity log
    if sa["active"]:
        # Header: spinner + tool count / token summary
        parts = []
        if sa["tool_count"]:
            parts.append(f"{sa['tool_count']} tools")
        if sa["token_info"]:
            parts.append(sa["token_info"])
        detail = " | ".join(parts) if parts else "running"
        detail = f"subagent: {detail}"

        spinner_frame = ProgressState.SPINNER_FRAMES[
            sa["spinner_frame_index"] % len(ProgressState.SPINNER_FRAMES)
        ]
        header = _truncate_plain(f"{spinner_frame} {detail}")
        frame_part, _, detail_part = header.partition(" ")
        lines = [f'<style fg="cyan">{_escape_html(frame_part)}</style> <style fg="white">{_escape_html(detail_part)}</style>']

        # Activity log — preserve stacked tool/result lines from panel messages.
        for event in sa.get("activity_log", []):
            # Strip Rich markup for toolbar display
            event_clean = re.sub(r'\[/?[^\]]*\]', '', event)
            for event_line in event_clean.splitlines():
                event_line = event_line.strip()
                if event_line:
                    lines.append(_style_line(event_line, "#888888"))

        return '\n'.join(lines) + '\n'

    # Subagent done — auto-dismiss after 3 seconds
    if sa["done_state"]:
        done_at = sa.get("done_at")
        if done_at and (time.monotonic() - done_at) > 3.0:
            progress.clear_subagent()
            return ""
        if sa["done_state"] == "complete":
            progress.clear_subagent()
            return ""
        else:
            return '<style fg="red">\u2717 subagent error</style> '

    return ""


def _get_normal_status_text(chat_manager, include_progress: bool = True):
    """Return the full normal status toolbar text (model, tokens, cost, swarm).

    Returns a string starting with ``\\n`` suitable for concatenating after
    interaction content and wrapping in ``HTML()``.
    """
    # Above-status progress: generic spinner + active tool
    progress_above = _get_progress_above_text(chat_manager) if include_progress else ""

    val = APPROVE_MODE_LABELS.get(chat_manager.approve_mode, chat_manager.approve_mode.upper())

    # Color approval mode: safe=green, accept_edits=muted gold
    _approve_color = {"safe": "#78B373", "accept_edits": "#B8A040"}.get(
        chat_manager.approve_mode, "#606060"
    )

    width = _toolbar_width()
    _status_line, status_html = _build_status_line(chat_manager, val, width)

    # Inject color into the approval value within the rendered HTML
    escaped_approval = _escape_html(f"Approval: {val}")
    colored_approval = f'Approval: <style fg="{_approve_color}">{_escape_html(val)}</style>'
    if escaped_approval in status_html:
        status_html = status_html.replace(escaped_approval, colored_approval, 1)
    
    # Keep the normal status toolbar visible during agent work. The progress
    # line is additive, not a replacement for model/approval/token status.
    if progress_above:
        toolbar_text = progress_above.rstrip('\n') + '\n' + status_html
    else:
        toolbar_text = status_html

    # Below-status: subagent active / done
    progress_below = _get_progress_below_text(chat_manager) if include_progress else ""
    if progress_below:
        toolbar_text += '\n' + _separator_line(width) + '\n' + progress_below

    # Append live swarm status — exactly one selected page — when in swarm admin mode.
    try:
        if getattr(chat_manager, 'swarm_admin_mode', False) and getattr(chat_manager, 'swarm_server', None):
            from ui.swarm_formatting import (
                format_swarm_toolbar_lines,
                format_task_list_toolbar_line,
            )

            page = getattr(chat_manager, 'swarm_status_page', 0)
            snapshot = chat_manager.swarm_server.status_snapshot()

            # Page separator line.
            page_names = ["Workers", "Plan"]
            total_pages = len(page_names)
            page = min(max(page, 0), total_pages - 1)
            toolbar_text += '\n' + _separator_line(width)

            if page == 0:
                # Page 1 — Workers.
                swarm_lines = format_swarm_toolbar_lines(snapshot)
                if swarm_lines:
                    for line in swarm_lines:
                        toolbar_text += '\n' + _style_line(line, "#888888", width)

            elif page == 1:
                # Page 2 — Plan (full checklist).
                task_list = getattr(chat_manager, 'task_list', None)
                swarm_complete = getattr(chat_manager, 'swarm_complete', False)
                swarm_complete_summary = getattr(chat_manager, 'swarm_complete_summary', "")
                plan_lines = format_task_list_toolbar_line(
                    task_list,
                    snapshot=snapshot,
                    title=getattr(chat_manager, 'task_list_title', None),
                    swarm_complete=swarm_complete,
                    swarm_complete_summary=swarm_complete_summary,
                    plan_map=getattr(chat_manager, '_swarm_task_plan_map', None),
                )
                if plan_lines:
                    for line in plan_lines:
                        toolbar_text += '\n' + _style_task_toolbar_line(line, width)


    except Exception:
        pass  # Never crash the toolbar

    # Non-swarm task list: show plan checklist below status bar in normal mode.
    try:
        if not getattr(chat_manager, 'swarm_admin_mode', False):
            task_list = getattr(chat_manager, 'task_list', None)
            if task_list:
                from ui.swarm_formatting import format_task_list_toolbar_line
                plan_lines = format_task_list_toolbar_line(
                    task_list,
                    snapshot=None,
                    title=getattr(chat_manager, 'task_list_title', None),
                    swarm_complete=False,
                    swarm_complete_summary="",
                    plan_map=None,
                )
                if plan_lines:
                    toolbar_text += '\n' + _separator_line(width)
                    for line in plan_lines:
                        toolbar_text += '\n' + _style_task_toolbar_line(line, width)
    except Exception:
        pass  # Never crash the toolbar

    return toolbar_text


TOOLBAR_STYLE = Style.from_dict({
    "bottom-toolbar": "bg:default fg:#FFFFFF noreverse",
    "bottom-toolbar.text": "bg:default fg:#FFFFFF noreverse",
})


def setup_common_bindings(chat_manager):
    """Create KeyBindings with shared logic (e.g., Shift+Tab for mode cycling).

    Toolbar interaction key dispatch is registered first so it takes
    priority over other bindings when an interaction is active.
    """
    bindings = KeyBindings()

    # --- Toolbar interaction key dispatch -----------------------------------
    # Each handler is gated behind a Condition filter so it is only active
    # while a toolbar interaction exists.  When no interaction is active the
    # keys are untouched and prompt_toolkit's default behaviour (history
    # navigation, submission, etc.) works normally.  This avoids the
    # "registered handler swallows the key" problem — in prompt_toolkit a
    # bound handler owns its key; simply returning does not fall through.

    _interaction_active = Condition(lambda: get_active_interaction(chat_manager) is not None)

    @bindings.add('up', filter=_interaction_active)
    def _tb_up(event):
        dispatch_toolbar_key(event, chat_manager)

    @bindings.add('down', filter=_interaction_active)
    def _tb_down(event):
        dispatch_toolbar_key(event, chat_manager)

    @bindings.add('left', filter=_interaction_active)
    def _tb_left(event):
        dispatch_toolbar_key(event, chat_manager)

    @bindings.add('right', filter=_interaction_active)
    def _tb_right(event):
        dispatch_toolbar_key(event, chat_manager)

    @bindings.add('enter', filter=_interaction_active)
    def _tb_enter(event):
        dispatch_toolbar_key(event, chat_manager)

    @bindings.add('escape', filter=_interaction_active, eager=True)
    def _tb_escape(event):
        dispatch_toolbar_key(event, chat_manager)

    @bindings.add('space', filter=_interaction_active)
    def _tb_space(event):
        dispatch_toolbar_key(event, chat_manager)

    @bindings.add('backspace', filter=_interaction_active)
    def _tb_backspace(event):
        dispatch_toolbar_key(event, chat_manager)

    @bindings.add('delete', filter=_interaction_active)
    def _tb_delete(event):
        dispatch_toolbar_key(event, chat_manager)

    @bindings.add('tab', filter=_interaction_active)
    def _tb_tab(event):
        dispatch_toolbar_key(event, chat_manager)

    @bindings.add('<any>', filter=_interaction_active)
    def _tb_any(event):
        """Forward printable characters to the active toolbar interaction.

        This handler only matches when an interaction is active (Condition
        filter).  It always consumes the event — the interaction owns all
        printable input while it is active.
        """
        dispatch_toolbar_key(event, chat_manager)

    # --- Existing bindings --------------------------------------------------

    @bindings.add('s-tab')
    def toggle_approve_mode(event):
        """Toggle between modes using Shift+Tab (blocked during thinking)."""
        # Import here to avoid circular imports and get current state
        import importlib
        main_module = importlib.import_module('ui.main')
        if main_module.INPUT_BLOCKED.get('blocked', False):
            return
        chat_manager.cycle_approve_mode()
        event.app.invalidate()
    
    return bindings
