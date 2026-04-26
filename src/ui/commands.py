"""Command routing and help display."""

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Optional
from llm import config

from core.config_manager import ConfigManager as ConfigManagerClass
from ui.displays import show_help_table, show_cron_help_table, show_skills_help_table
from ui.banner import display_startup_banner
from core.agentic import SubAgentPanel
from ui.setting_selector import SettingSelector, SettingCategory, SettingOption

from utils.settings import MonokaiDarkBGStyle, context_settings, left_align_headings, tool_settings
from rich.markdown import Markdown
from rich.table import Table

from rich import box
from pathlib import Path
import json
import logging
import ssl
import urllib.request
import urllib.error
from utils.validation import validate_api_url


logger = logging.getLogger(__name__)

# Global ConfigManager instance
config_manager = ConfigManagerClass()


@dataclass
class CommandResult:
    """Standardized command return type."""
    status: str  # "exit", "handled", or "continue"
    replacement_input: Optional[str] = None  # For /edit command

# Command handler functions

def _handle_exit(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle exit command."""
    return CommandResult(status="exit")


def _handle_setup(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Re-run the first-run setup wizard."""
    from ui.setup_wizard import run_wizard as _run_setup_wizard
    _run_setup_wizard(console)
    # Reload config so changes take effect immediately
    try:
        from llm import config as llm_config
        llm_config.reload_config()
    except Exception:
        pass
    return CommandResult(status="handled")


_CRON_ADD_EXAMPLES = [
    "[dim]  /cron add morning_brief weekdays at 8am Give me a morning briefing[/dim]",
    "[dim]  /cron add cleanup every 1 hour Clean up temp files[/dim]",
    "[dim]  /cron add news daily at 5am Draft an email with AI news[/dim]",
]


def _print_cron_add_usage(console, prefix=None):
    """Print /cron add usage and examples."""
    if prefix:
        console.print(prefix)
    console.print("[red]Usage: /cron add <id> <schedule> <command>[/red]")
    console.print("[dim]Examples:[/dim]")
    for line in _CRON_ADD_EXAMPLES:
        console.print(line)


def _cron_list(console, cron_config):
    """Display all cron jobs in a table."""
    if not cron_config.jobs:
        console.print("[dim]No cron jobs defined. Use [bold #5F9EA0]/cron help[/bold #5F9EA0] to get started.[/dim]")
        return CommandResult(status="handled")

    table = Table(
        title="Cron Jobs",
        box=box.SIMPLE_HEAVY,
        title_style="bold #5F9EA0",
        border_style="grey23",
        show_header=True,
        header_style="bold",
    )
    table.add_column("ID", style="bold white")
    table.add_column("Schedule")
    table.add_column("Command", max_width=40, no_wrap=False)
    table.add_column("Status", justify="center")
    table.add_column("Last Run", justify="right")

    for job in cron_config.jobs.values():
        status = "[green]ON[/green]" if job.enabled else "[red]OFF[/red]"
        last = job.last_run or "[dim]never[/dim]"
        cmd_display = job.command[:37] + "..." if len(job.command) > 40 else job.command
        table.add_row(job.id, job.schedule, cmd_display, status, last)

    console.print(table)
    return CommandResult(status="handled")


def _cron_add(console, sub_args, cron_config, notify_scheduler):
    """Add a new cron job."""
    from core.cron import CronJob, parse_schedule

    tokens = sub_args.strip().split()
    if len(tokens) < 3:
        _print_cron_add_usage(console)
        return CommandResult(status="handled")

    job_id = tokens[0]
    # Greedily consume tokens for the schedule (min 1, max 6 words)
    schedule = None
    cmd_start = 2
    for end in range(2, min(len(tokens) + 1, 8)):
        candidate = " ".join(tokens[1:end])
        try:
            parse_schedule(candidate)
            schedule = candidate
            cmd_start = end
            break
        except ValueError:
            continue

    if schedule is None:
        attempted = " ".join(tokens[1:min(len(tokens), 7)])
        _print_cron_add_usage(console, prefix=f"[red]Cannot parse schedule: '{attempted}'[/red]")
        return CommandResult(status="handled")

    command = " ".join(tokens[cmd_start:])

    if not re.match(r'^[a-zA-Z0-9_-]+$', job_id):
        console.print(f"[red]Invalid job ID '{job_id}'. Use only letters, numbers, underscores, and hyphens.[/red]")
        return CommandResult(status="handled")

    if job_id in cron_config.jobs:
        console.print(f"[red]Job '{job_id}' already exists. Remove it first or use a different ID.[/red]")
        return CommandResult(status="handled")

    job = CronJob(id=job_id, schedule=schedule, command=command)
    cron_config.add_job(job)
    notify_scheduler()
    console.print(f"[green]Added cron job '{job_id}' (schedule: {schedule})[/green]")
    return CommandResult(status="handled")


def _cron_remove(console, sub_args, cron_config, notify_scheduler):
    """Remove a cron job by ID."""
    job_id = sub_args.strip()
    if not job_id:
        console.print("[red]Usage: /cron remove <id>[/red]")
        return CommandResult(status="handled")
    if job_id == "dream":
        console.print("[red]The 'dream' job is managed by DREAM_SETTINGS.enabled in config.yaml and cannot be removed.[/red]")
        return CommandResult(status="handled")
    if cron_config.remove_job(job_id):
        notify_scheduler()
        console.print(f"[green]Removed cron job '{job_id}'[/green]")
    else:
        console.print(f"[red]Job '{job_id}' not found[/red]")
    return CommandResult(status="handled")


def _cron_toggle(console, sub_args, cron_config, notify_scheduler, enable):
    """Enable or disable a cron job."""
    verb = "enable" if enable else "disable"
    style = "green" if enable else "yellow"
    job_id = sub_args.strip()
    if not job_id:
        console.print(f"[red]Usage: /cron {verb} <id>[/red]")
        return CommandResult(status="handled")
    if not enable and job_id == "dream":
        console.print("[red]The 'dream' job is managed by DREAM_SETTINGS.enabled in config.yaml and cannot be disabled via /cron.[/red]")
        return CommandResult(status="handled")
    if job_id in cron_config.jobs:
        cron_config.update_job(job_id, enabled=enable)
        notify_scheduler()
        console.print(f"[{style}]{verb.title()}d '{job_id}'[/{style}]")
    else:
        console.print(f"[red]Job '{job_id}' not found[/red]")
    return CommandResult(status="handled")


def _cron_run(console, sub_args, cron_config):
    """Manually trigger a cron job immediately."""
    from core.cron import run_single_job
    from datetime import datetime

    job_id = sub_args.strip()
    if not job_id:
        console.print("[red]Usage: /cron run <id>[/red]")
        return CommandResult(status="handled")
    job = cron_config.get_job(job_id)
    if not job:
        console.print(f"[red]Job '{job_id}' not found[/red]")
        return CommandResult(status="handled")
    console.print(f"[#5F9EA0]Running cron job '{job_id}'...[/#5F9EA0]")
    try:
        run_single_job(job, console=console, interactive=True)
        cron_config.update_job(job_id, last_run=datetime.now().isoformat(), last_status="ok")
        console.print(f"[green]Job '{job_id}' completed.[/green]")
    except Exception as e:
        cron_config.update_job(job_id, last_status="error")
        console.print(f"[red]Job '{job_id}' failed: {e}[/red]")
    return CommandResult(status="handled")


def _cron_allowlist(console, sub_args):
    """Manage cron command allowlists."""
    from core.cron_allowlist import CronAllowlist
    allowlist = CronAllowlist()

    allow_parts = sub_args.strip().split(maxsplit=1)
    allow_sub = allow_parts[0].lower() if allow_parts else ""
    allow_rest = allow_parts[1] if len(allow_parts) > 1 else ""

    if allow_sub == "list" and allow_rest:
        jid = allow_rest.strip()
        cmds = allowlist.get_commands(jid)
        if not cmds:
            console.print(f"[dim]No allowed commands for '{jid}'.[/dim]")
            return CommandResult(status="handled")
        console.print(f"[bold]{jid}[/bold] ({len(cmds)} command{'s' if len(cmds) != 1 else ''})")
        for cmd in cmds:
            console.print(f"  [green]✓[/green] {cmd}")
        return CommandResult(status="handled")

    if not allow_sub or allow_sub == "list":
        all_jobs = allowlist.all_jobs()
        if not all_jobs:
            console.print("[dim]No cron command allow lists. Run '/cron run <id>' to build one.[/dim]")
            return CommandResult(status="handled")
        for jid, cmds in all_jobs.items():
            console.print(f"[bold]{jid}[/bold] ({len(cmds)} command{'s' if len(cmds) != 1 else ''})")
            if cmds:
                for cmd in cmds:
                    console.print(f"  [green]✓[/green] {cmd}")
            else:
                console.print("  [dim](empty)[/dim]")
        return CommandResult(status="handled")

    if allow_sub == "add":
        tokens = allow_rest.strip().split(maxsplit=1)
        if len(tokens) < 2:
            console.print("[red]Usage: /cron allowlist add <job_id> <command>[/red]")
            return CommandResult(status="handled")
        jid, cmd = tokens[0], tokens[1]
        if allowlist.add_command(jid, cmd):
            console.print(f"[green]Added '{cmd}' to '{jid}' allow list.[/green]")
        else:
            console.print(f"[dim]Command already in '{jid}' allow list.[/dim]")
        return CommandResult(status="handled")

    if allow_sub in ("remove", "rm"):
        tokens = allow_rest.strip().split(maxsplit=1)
        if len(tokens) < 2:
            console.print("[red]Usage: /cron allowlist remove <job_id> <command>[/red]")
            return CommandResult(status="handled")
        jid, cmd = tokens[0], tokens[1]
        if allowlist.remove_command(jid, cmd):
            console.print(f"[green]Removed '{cmd}' from '{jid}' allow list.[/green]")
        else:
            console.print(f"[red]Command not found in '{jid}' allow list.[/red]")
        return CommandResult(status="handled")

    if allow_sub == "clear":
        jid = allow_rest.strip()
        if not jid:
            console.print("[red]Usage: /cron allowlist clear <job_id>[/red]")
            return CommandResult(status="handled")
        count = allowlist.clear_job(jid)
        console.print(f"[green]Cleared {count} command{'s' if count != 1 else ''} from '{jid}' allow list.[/green]")
        return CommandResult(status="handled")

    console.print(f"[red]Unknown allowlist sub-command: '{allow_sub}'[/red]")
    console.print("[dim]Sub-commands: list [job_id], add, remove, clear[/dim]")
    return CommandResult(status="handled")


def _handle_cron(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Manage cron jobs: list, add, remove, enable, disable."""
    from core.cron import CronConfig

    if cron_scheduler is not None:
        cron_config = cron_scheduler.config
    else:
        cron_config = CronConfig()

    def notify_scheduler():
        """Immediately reload scheduler config after a mutation."""
        if cron_scheduler is not None:
            cron_scheduler.reload()

    if not args or args.strip() == "list":
        return _cron_list(console, cron_config)

    parts = args.strip().split(maxsplit=1)
    sub_cmd = parts[0].lower()
    sub_args = parts[1] if len(parts) > 1 else ""

    dispatch = {
        "add": lambda: _cron_add(console, sub_args, cron_config, notify_scheduler),
        "remove": lambda: _cron_remove(console, sub_args, cron_config, notify_scheduler),
        "rm": lambda: _cron_remove(console, sub_args, cron_config, notify_scheduler),
        "enable": lambda: _cron_toggle(console, sub_args, cron_config, notify_scheduler, enable=True),
        "disable": lambda: _cron_toggle(console, sub_args, cron_config, notify_scheduler, enable=False),
        "run": lambda: _cron_run(console, sub_args, cron_config),
        "allowlist": lambda: _cron_allowlist(console, sub_args),
        "help": lambda: show_cron_help_table(console) or CommandResult(status="handled"),
    }

    handler = dispatch.get(sub_cmd)
    if handler:
        return handler()

    console.print(f"[red]Unknown cron sub-command: '{sub_cmd}'[/red]")
    console.print("[dim]Sub-commands: list, add, remove, enable, disable, run, allowlist, help[/dim]")
    return CommandResult(status="handled")


def _handle_help(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle help command."""
    show_help_table(console)
    return CommandResult(status="handled")


def _handle_compact(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle manual context compaction."""
    # Show current context summary immediately using the same format as the status bar
    num_messages = len(chat_manager.messages)
    tokens_curr = chat_manager.token_tracker.current_context_tokens
    console.print(
        "Current context summary:"
        f"\n  Messages: {num_messages}"
        f"\n  Curr: {tokens_curr:,}"
    )
    console.print()  # Spacer line

    result = chat_manager.compact_history(console=console, trigger="manual")
    if not result:
        console.print("[yellow]Nothing to compact.[/yellow]")
        return CommandResult(status="handled")

    console.print(
        f"[green]Session reset: "
        f"{result['before_tokens']:,} -> {result['after_tokens']:,} tokens[/green]"
    )
    
    # Show the compacted summary in debug mode
    if debug_mode_container.get('debug') and 'summary' in result:
        console.print()
        console.print("[#5F9EA0]Compacted summary:[/#5F9EA0]")
        console.print(f"[dim]{result['summary']}[/dim]")
    return CommandResult(status="handled")


def _handle_config(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle config command - interactive runtime settings editor."""
    from ui.setting_selector import SettingOption, SettingCategory, SettingSelector

    # Build runtime settings from current state
    runtime_settings = [
        SettingOption(
            key="debug", text="Debug Mode",
            value=bool(debug_mode_container.get("debug")),
            input_type="boolean",
            on_text="ON", off_text="OFF",
        ),
        SettingOption(
            key="logging", text="Conversation Logging",
            value=bool(chat_manager.markdown_logger),
            input_type="boolean",
            on_text="ON", off_text="OFF",
        ),
        SettingOption(
            key="approve", text="Approval Mode",
            value=chat_manager.approve_mode,
            input_type="select",
            options=[
                {"value": "safe", "text": "SAFE"},
                {"value": "accept_edits", "text": "ACCEPT EDITS"},
                {"value": "danger", "text": "DANGER"},
            ],
        ),
        SettingOption(
            key="memory_enabled", text="Memory",
            value=config.MEMORY_SETTINGS.get("enabled", True),
            input_type="boolean",
            on_text="ON", off_text="OFF",
        ),
    ]

    # Build status bar settings
    sb_config = config.STATUS_BAR_SETTINGS
    sb_settings = [
        SettingOption(
            key="show_curr_tokens", text="Current context tokens",
            value=sb_config.get("show_curr_tokens", True), input_type="boolean",
        ),
        SettingOption(
            key="show_in_tokens", text="Total prompt tokens",
            value=sb_config.get("show_in_tokens", True), input_type="boolean",
        ),
        SettingOption(
            key="show_out_tokens", text="Total completion tokens",
            value=sb_config.get("show_out_tokens", True), input_type="boolean",
        ),
        SettingOption(
            key="show_total_tokens", text="Total session tokens",
            value=sb_config.get("show_total_tokens", True), input_type="boolean",
        ),
        SettingOption(
            key="show_cost", text="Session cost",
            value=sb_config.get("show_cost", True), input_type="boolean",
        ),
    ]

    # Build context/compaction settings
    ctx_settings = [
        SettingOption(
            key="compact_trigger_tokens", text="Compaction Threshold",
            value=context_settings.compact_trigger_tokens,
            input_type="number",
        ),
        SettingOption(
            key="enable_tool_compaction", text="Per-Message Tool Compaction",
            value=context_settings.tool_compaction.enable_per_message_compaction,
            input_type="boolean",
            on_text="ON", off_text="OFF",
        ),
        SettingOption(
            key="uncompacted_tail_tokens", text="Uncompacted Tail Tokens",
            value=context_settings.tool_compaction.uncompacted_tail_tokens,
            input_type="number",
        ),
        SettingOption(
            key="min_tool_blocks", text="Min Tool Blocks Preserved",
            value=context_settings.tool_compaction.min_tool_blocks,
            input_type="number",
        ),
    ]

    categories = [
        SettingCategory(title="Runtime Settings", settings=runtime_settings),
        SettingCategory(title="Context Settings", settings=ctx_settings),
        SettingCategory(title="Status Bar Items", settings=sb_settings),
    ]
    selector = SettingSelector(
        categories=categories,
        title="Configuration",
    )

    changes = selector.run()

    # Selector manages its own rendering; just print a separator on dismissal
    console.print()

    if changes is None:
        console.print("[dim]Cancelled.[/dim]")
        return CommandResult(status="handled")

    if not changes:
        console.print("[dim]No changes made.[/dim]")
        return CommandResult(status="handled")

    # Apply changes
    change_lines = []
    sb_changes = {}
    sb_labels = {s.key: s.text for s in sb_settings}
    for key, value in changes.items():
        if key == "debug":
            debug_mode_container['debug'] = value
            state = "enabled" if value else "disabled"
            change_lines.append(f"  Debug Mode: {state}")
        elif key == "logging":
            chat_manager.set_logging(value)
            state = "enabled" if value else "disabled"
            change_lines.append(f"  Conversation Logging: {state}")
        elif key == "approve":
            chat_manager.approve_mode = value
            labels = {"safe": "SAFE", "accept_edits": "ACCEPT EDITS", "danger": "DANGER"}
            change_lines.append(f"  Approval Mode: {labels.get(value, value.upper())}")
            if value == "danger":
                console.print()
                console.print("[bold red on default]  WARNING: DANGER MODE ENABLED[/bold red on default]")
                console.print("[bold red on default]  All commands will be auto-approved.[/bold red on default]")
                console.print("[bold red on default]  Dangerous git commands are still blocked.[/bold red on default]")
                console.print("[bold yellow on default]  Use at your own risk![/bold yellow on default]")
                console.print()
        elif key == "memory_enabled":
            config.update_memory_settings({"enabled": value})
            state = "enabled" if value else "disabled"
            change_lines.append(f"  Memory: {state}")
        elif key == "compact_trigger_tokens":
            context_settings.compact_trigger_tokens = int(value)
            change_lines.append(f"  Compaction Threshold: {value:,} tokens")
        elif key == "enable_tool_compaction":
            context_settings.tool_compaction.enable_per_message_compaction = value
            state = "enabled" if value else "disabled"
            change_lines.append(f"  Per-Message Tool Compaction: {state}")
        elif key == "uncompacted_tail_tokens":
            context_settings.tool_compaction.uncompacted_tail_tokens = int(value)
            change_lines.append(f"  Uncompacted Tail Tokens: {value:,}")
        elif key == "min_tool_blocks":
            context_settings.tool_compaction.min_tool_blocks = int(value)
            change_lines.append(f"  Min Tool Blocks Preserved: {value}")
        elif key in sb_labels:
            sb_changes[key] = value
            state = "ON" if value else "OFF"
            change_lines.append(f"  {sb_labels[key]}: {state}")

    # Persist context setting changes to config
    ctx_changes = {k: v for k, v in changes.items() if k in ("compact_trigger_tokens", "enable_tool_compaction", "uncompacted_tail_tokens", "min_tool_blocks")}
    if ctx_changes:
        try:
            cfg_data = config_manager.load(force_reload=True)
            if "CONTEXT_SETTINGS" not in cfg_data:
                cfg_data["CONTEXT_SETTINGS"] = {}
            if "tool_compaction" not in cfg_data["CONTEXT_SETTINGS"]:
                cfg_data["CONTEXT_SETTINGS"]["tool_compaction"] = {}
            if "compact_trigger_tokens" in ctx_changes:
                cfg_data["CONTEXT_SETTINGS"]["compact_trigger_tokens"] = int(ctx_changes["compact_trigger_tokens"])
            if "enable_tool_compaction" in ctx_changes:
                cfg_data["CONTEXT_SETTINGS"]["tool_compaction"]["enable_per_message_compaction"] = ctx_changes["enable_tool_compaction"]
            if "uncompacted_tail_tokens" in ctx_changes:
                cfg_data["CONTEXT_SETTINGS"]["tool_compaction"]["uncompacted_tail_tokens"] = int(ctx_changes["uncompacted_tail_tokens"])
            if "min_tool_blocks" in ctx_changes:
                cfg_data["CONTEXT_SETTINGS"]["tool_compaction"]["min_tool_blocks"] = int(ctx_changes["min_tool_blocks"])
            config_manager.save(cfg_data)
        except Exception as e:
            console.print(f"[red]Failed to save context settings: {e}[/red]")

    # Persist status bar changes to config
    if sb_changes:
        config.update_status_bar_settings(sb_changes)
        try:
            cfg_data = config_manager.load(force_reload=True)
            if "STATUS_BAR_SETTINGS" not in cfg_data:
                cfg_data["STATUS_BAR_SETTINGS"] = {}
            cfg_data["STATUS_BAR_SETTINGS"].update(sb_changes)
            config_manager.save(cfg_data)
        except Exception as e:
            console.print(f"[red]Failed to save status bar settings: {e}[/red]")

    # Persist memory setting to config
    if "memory_enabled" in changes:
        try:
            cfg_data = config_manager.load(force_reload=True)
            if "MEMORY_SETTINGS" not in cfg_data:
                cfg_data["MEMORY_SETTINGS"] = {}
            cfg_data["MEMORY_SETTINGS"]["enabled"] = changes["memory_enabled"]
            config_manager.save(cfg_data)
        except Exception as e:
            console.print(f"[red]Failed to save memory settings: {e}[/red]")

    # Display summary
    console.print(f"[green]Settings updated:[/green]")
    for line in change_lines:
        console.print(line)

    return CommandResult(status="handled")


def _handle_clear(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle clear/reset command."""
    # Display conversation cost for the previous chat
    costs = config_manager.get_usage_costs()

    # Display token summary for the previous chat
    current_tokens = chat_manager.token_tracker.current_context_tokens
    conv_in = chat_manager.token_tracker.conv_prompt_tokens
    conv_out = chat_manager.token_tracker.conv_completion_tokens
    conv_total = chat_manager.token_tracker.conv_total_tokens

    console.print()
    console.print("Conversation Summary:")
    console.print(f"  Current Context: {current_tokens:,} tokens")
    console.print(f"  In: {conv_in:,} tokens")
    console.print(f"  Out: {conv_out:,} tokens")
    console.print(f"  Total: {conv_total:,} tokens")

    # Show cache token breakdown if any cache was used
    conv_cache_read = chat_manager.token_tracker.conv_cache_read_tokens
    conv_cache_creation = chat_manager.token_tracker.conv_cache_creation_tokens
    if conv_cache_read > 0 or conv_cache_creation > 0:
        total_cached = conv_cache_read + conv_cache_creation
        cache_activity_read_pct = (
            conv_cache_read / total_cached * 100
        ) if total_cached > 0 else 0
        cache_coverage_pct = (
            conv_cache_read / conv_in * 100
        ) if conv_in > 0 else 0
        console.print(f"  Cache read:   {conv_cache_read:,} tokens")
        console.print(f"  Cache write:  {conv_cache_creation:,} tokens")
        console.print(f"  ({cache_coverage_pct:.0f}% input cached, {cache_activity_read_pct:.0f}% cache reads)")

    # Display cost — combined actual + estimated, with config-based fallback
    tracker_conv = chat_manager.token_tracker
    if tracker_conv.has_actual_cost():
        conv_cost = tracker_conv.conv_actual_cost + tracker_conv.conv_estimated_cost
    else:
        conv_cost = tracker_conv.get_conversation_display_cost(costs['in'], costs['out'])
    if conv_cost > 0:
        console.print(f"  Cost: ${conv_cost:.4f}")

    console.print()

    chat_manager.reset_session()
    display_startup_banner(chat_manager.approve_mode, clear_screen=True)
    return CommandResult(status="handled")


def _open_provider_editor(chat_manager, console, provider):
    """Open interactive setting editor for a specific provider.

    Args:
        chat_manager: ChatManager instance
        console: Rich console for output
        provider: Provider name (e.g. 'openrouter', 'glm')

    Returns:
        True if settings were saved, False if cancelled
    """
    from ui.setting_selector import SettingOption, SettingCategory, SettingSelector

    cfg = config.get_provider_config(provider)
    config_data = config_manager.load()
    settings = []

    # Model setting
    current_model = cfg.get('model') or cfg.get('api_model') or ''
    model_label = "Model path" if provider == "local" else "Model"
    settings.append(SettingOption(
        key="model", text=model_label,
        value=current_model, input_type="text",
    ))

    # API key (not for local or bone — bone manages its own key via /signup)
    if provider not in ("local", "bone"):
        current_key = cfg.get('api_key', '')
        # Show masked value, store actual in description for comparison
        masked = (current_key[:8] + "...") if len(current_key) > 8 else (current_key or "")
        settings.append(SettingOption(
            key="api_key", text="API Key",
            value=masked, input_type="text",
            description=current_key,
        ))

    # Cost in/out (not for local or bone — costs are server-side)
    if provider not in ("local", "bone"):
        model_prices = config_data.get("MODEL_PRICES", {})
        existing = model_prices.get(current_model, {})
        settings.append(SettingOption(
            key="cost_in", text="Cost in ($/1M tokens)",
            value=existing.get('cost_in', 0.0), input_type="float",
            min_val=0.0, step=0.01,
        ))
        settings.append(SettingOption(
            key="cost_out", text="Cost out ($/1M tokens)",
            value=existing.get('cost_out', 0.0), input_type="float",
            min_val=0.0, step=0.01,
        ))

    provider_label = config.get_provider_display_name(provider)
    category = SettingCategory(title=f"{provider_label} Settings", settings=settings)

    selector = SettingSelector(
        categories=[category],
        title=f"Configure {provider_label}",
    )

    changes = selector.run()

    if changes is None:
        console.print("[dim]No changes made.[/dim]")
        return False

    # Apply changes
    change_lines = []

    if "model" in changes and changes["model"]:
        try:
            config_manager.set_model(provider, changes["model"])
            change_lines.append(f"  Model: {changes['model']}")
        except Exception as e:
            console.print(f"[red]Failed to set model: {e}[/red]")

    if "api_key" in changes and changes["api_key"]:
        # Don't re-save if the user didn't actually change it (masked display)
        api_key_input = changes["api_key"]
        original_key = cfg.get('api_key', '')
        # Detect if user typed a real key (longer than masked display or different)
        if api_key_input != original_key and not api_key_input.endswith("..."):
            try:
                config_manager.set_api_key(provider, api_key_input)
                masked = (api_key_input[:8] + "...") if len(api_key_input) > 8 else api_key_input
                change_lines.append(f"  API Key: {masked}")
            except Exception as e:
                console.print(f"[red]Failed to set API key: {e}[/red]")

    if "cost_in" in changes or "cost_out" in changes:
        model_name = changes.get("model") or current_model
        if model_name:
            # Use changed values, falling back to originals (not 0.0)
            existing_prices = config_data.get("MODEL_PRICES", {}).get(model_name, {})
            cost_in = changes.get("cost_in", existing_prices.get("cost_in", 0.0))
            cost_out = changes.get("cost_out", existing_prices.get("cost_out", 0.0))
            try:
                config_manager.set_model_price(model_name, cost_in, cost_out)
                change_lines.append(f"  Cost: ${cost_in:.2f}/${cost_out:.2f} per 1M tokens")
            except Exception as e:
                console.print(f"[red]Failed to set pricing: {e}[/red]")

    # Reload config and switch provider
    config_manager.set_provider(provider)
    chat_manager.reload_config()
    result = chat_manager.switch_provider(provider)

    if change_lines:
        console.print(f"[green]{provider} updated:[/green]")
        for line in change_lines:
            console.print(line)
    else:
        console.print(f"[green]{provider} activated.[/green]")

    if "Failed" not in result and "failed" not in result:
        console.print(f"[dim]{result}[/dim]")

    return True


def _handle_provider(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle provider switching and configuration command."""
    current = getattr(chat_manager.client, 'provider', 'unknown')

    if args:
        provider = args.strip().lower()

        # Validate provider name
        if provider not in config.get_providers():
            console.print(f"[red]Error: Unknown provider '{provider}'[/red]")
            available = ', '.join(config.get_provider_display_name(prov) for prov in config.get_providers())
            console.print(f"[dim]Available providers: {available}[/dim]")
            return CommandResult(status="handled")

        # Switch directly to the named provider
        if provider == current:
            console.print(f"[dim]Already on {provider}[/dim]")
            return CommandResult(status="handled")

        config_manager.set_provider(provider)
        chat_manager.reload_config()
        result = chat_manager.switch_provider(provider)

        cfg = config.get_provider_config(provider)
        model = cfg.get('model') or cfg.get('api_model') or ''
        label = config.get_provider_display_name(provider)
        if model:
            label += f" ({model})"
        console.print(f"[green]Switched to {label}[/green]")
        if "Failed" not in result and "failed" not in result:
            console.print(f"[dim]{result}[/dim]")

        return CommandResult(status="handled")
    else:
        # Show all providers as a radio-button list (same style as model selector)
        from ui.setting_selector import SettingOption, SettingCategory, SettingSelector

        provider_options = []
        for prov in config.get_providers():
            cfg = config.get_provider_config(prov)
            model = cfg.get('model') or cfg.get('api_model') or ''
            entry = {"value": prov, "text": config.get_provider_display_name(prov)}
            if model:
                entry["description"] = model[:40]
            provider_options.append(entry)

        provider_setting = SettingOption(
            key="provider",
            text="Select Provider",
            value=current,
            input_type="options",
            options=provider_options,
        )

        selector = SettingSelector(
            categories=[SettingCategory(title="", settings=[provider_setting])],
            title="",
            show_save=False,
        )
        result = selector.run()

        if result is None:
            console.print("[dim]Cancelled.[/dim]")
            return CommandResult(status="handled")

        # Get selected provider (from changes, or current if unchanged)
        provider = result.get('provider', current)

    # Open interactive editor for the selected provider
    _open_provider_editor(chat_manager, console, provider)

    return CommandResult(status="handled")


def _handle_model(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle model setting command."""
    current_provider = getattr(chat_manager.client, 'provider', 'unknown')
    
    # For bone provider, show interactive model selection
    if current_provider == "bone" and not args:
        from ui.setting_selector import SettingOption, SettingCategory, SettingSelector
        
        cfg = config.get_provider_config(current_provider)
        current_model = cfg.get('model') or cfg.get('api_model') or ''
        
        # Models available via bone proxy (OpenRouter-compatible)
        # Format: (display_name, openrouter_model_id)
        bone_models = [
            # DeepSeek
            ("DeepSeek-V3.2       1×", "deepseek/deepseek-v3.2"),
            # MiniMax
            ("MiniMax-M2.5        1×", "minimax/minimax-m2.5"),
            ("MiniMax-M2.7        1.5×", "minimax/minimax-m2.7"),
            # Moonshot AI
            ("Kimi-K2.5           3×", "moonshotai/kimi-k2.5"),
            # xAI
            ("Grok-Code-Fast-1    1.5×", "x-ai/grok-code-fast-1"),
            ("Grok-4.1-Fast       1×", "x-ai/grok-4.1-fast"),
            # Z-AI
            ("GLM-4.5-Air (Free)  0×", "z-ai/glm-4.5-air:free"),
            ("GLM-4.7             3×", "z-ai/glm-4.7"),
            ("GLM-5               5×", "z-ai/glm-5"),
            ("GLM-5-Turbo         10×", "z-ai/glm-5-turbo"),
            ("GLM-5.1            10×", "z-ai/glm-5.1"),
        ]

        model_options = []
        active_value = current_model
        for display_name, model_id in bone_models:
            if model_id == current_model or display_name.lower() == current_model.lower():
                active_value = model_id
            model_options.append({
                "value": model_id,
                "text": display_name,
            })

        model_setting = SettingOption(
            key="model",
            text="Select Model",
            value=active_value,
            input_type="options",
            options=model_options,
        )
        
        selector = SettingSelector(
            categories=[SettingCategory(title="", settings=[model_setting])],
            title="",
            show_save=False,
        )
        result = selector.run()
        
        if result is None or not isinstance(result, dict) or 'model' not in result:
            console.print("[dim]Cancelled.[/dim]")
            return CommandResult(status="handled")
        
        model = result['model']
    elif not args:
        # Show current model for current provider
        cfg = config.get_provider_config(current_provider)
        model = cfg.get('model') or cfg.get('api_model') or 'Not set'
        console.print(f"[bold #5F9EA0]Current provider:[/bold #5F9EA0] {current_provider}")
        console.print(f"[bold #5F9EA0]Current model:[/bold #5F9EA0] {model}")
        return CommandResult(status="handled")
    else:
        model = args.strip()

    # Set model for current provider
    try:
        backup_path = config_manager.set_model(current_provider, model)
        console.print(f"[green]Model set to '{model}' for {current_provider} provider[/green]")
        if backup_path:
            console.print(f"[dim]Saved to config.json (backup: {backup_path.name})[/dim]")

        # Reload config and update client
        chat_manager.reload_config()
    except ValueError as e:
        console.print(f"[red]Error: {e}[/red]")
    except Exception as e:
        console.print(f"[red]Failed to set model: {e}[/red]")

    return CommandResult(status="handled")


def _handle_key(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle API key setting command."""
    if not args:
        # Show current API key status for current provider
        current_provider = getattr(chat_manager.client, 'provider', 'unknown')
        cfg = config.get_provider_config(current_provider)

        if current_provider == "local":
            console.print("[yellow]Local provider doesn't use API keys[/yellow]")
        else:
            api_key = cfg.get('api_key', '')
            if api_key:
                # Show masked API key
                masked = api_key[:8] + "..." if len(api_key) > 8 else "***"
                console.print(f"[bold #5F9EA0]Current provider:[/bold #5F9EA0] {current_provider}")
                console.print(f"[bold #5F9EA0]API key:[/bold #5F9EA0] {masked}")
            else:
                console.print(f"[bold #5F9EA0]Current provider:[/bold #5F9EA0] {current_provider}")
                console.print("[yellow]API key not set[/yellow]")
        return CommandResult(status="handled")

    api_key = args.strip()

    # Set API key for current provider
    current_provider = getattr(chat_manager.client, 'provider', 'unknown')

    if current_provider == "local":
        console.print("[yellow]Local provider doesn't use API keys[/yellow]")
        return CommandResult(status="handled")

    try:
        backup_path = config_manager.set_api_key(current_provider, api_key)
        console.print(f"[green]API key set for {current_provider} provider[/green]")
        if backup_path:
            console.print(f"[dim]Saved to config.json (backup: {backup_path.name})[/dim]")

        # Reload config and update client
        chat_manager.reload_config()
    except ValueError as e:
        console.print(f"[red]Error: {e}[/red]")
    except Exception as e:
        console.print(f"[red]Failed to set API key: {e}[/red]")

    return CommandResult(status="handled")



def _handle_edit(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle external editor command for multi-line input.

    Opens an external editor for composing prompts. After the editor closes,
    the content is sent to the LLM.

    Returns:
        CommandResult: status="handled" if cancelled/failed
                       status="continue" with replacement_input to send to LLM
    """
    from utils.editor import open_editor_for_input

    success, content = open_editor_for_input(
        console,
        debug_mode_container['debug']
    )

    if not success:
        # Error already displayed by open_editor_for_input
        return CommandResult(status="handled")

    # Check if content is empty
    if not content or not content.strip():
        console.print("[yellow]Editor closed with no content - cancelling[/yellow]")
        return CommandResult(status="handled")

    # Show summary
    lines = [line for line in content.split('\n') if line.strip()]
    word_count = len(content.split())
    console.print(f"[green]Received {len(lines)} lines ({word_count} words) from editor[/green]")

    # Return continue status to pass content to LLM
    return CommandResult(status="continue", replacement_input=content)





def _handle_usage(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle usage command - show/calculate token costs or set cost rates."""
    console.print()
    
    # Get current model
    current_model = getattr(chat_manager.client, 'model', '')
    
    if args:
        # Parse setting command: in|out <value>
        parts = args.split()
        
        if len(parts) != 2 or parts[0].lower() not in ['in', 'out']:
            console.print("[red]Usage: /usage in|out <cost>[/red]")
            console.print("[dim]Cost is per 1M tokens (e.g., 0.5 = $0.50 per 1M tokens)[/dim]")
            console.print("[dim]Examples:[/dim]")
            console.print(f"[dim]  /usage in 1.00       - Set input cost for current model ({current_model})[/dim]")
            console.print(f"[dim]  /usage out 3.20      - Set output cost for current model ({current_model})[/dim]")
            console.print()
            return CommandResult(status="handled")

        direction, value = parts
        direction = direction.lower()

        try:
            cost = float(value)
            if cost < 0:
                console.print("[red]Error: Cost must be non-negative[/red]")
                console.print()
                return CommandResult(status="handled")
        except ValueError:
            console.print("[red]Error: Cost must be a valid number[/red]")
            console.print()
            return CommandResult(status="handled")

        # Set appropriate cost for current model
        # Get existing prices for the model
        existing_prices = config_manager.get_model_price(current_model)
        cost_in = existing_prices['in']
        cost_out = existing_prices['out']
        
        if direction == 'in':
            cost_in = cost
        elif direction == 'out':
            cost_out = cost
        
        backup_path = config_manager.set_model_price(current_model, cost_in, cost_out)
        
        if direction == 'in':
            console.print(f"[green]Model '{current_model}' input token cost set to ${cost:.6f} per 1M tokens[/green]")
        else:
            console.print(f"[green]Model '{current_model}' output token cost set to ${cost:.6f} per 1M tokens[/green]")

        if backup_path:
            console.print(f"[dim]Saved to config.json (backup: {backup_path.name})[/dim]")

        console.print()
        return CommandResult(status="handled")

    # No args - show current usage stats
    current_provider = getattr(chat_manager.client, 'provider', 'unknown')

    # bone: fetch from proxy API
    if current_provider == "bone":
        cfg = config.get_provider_config("bone")
        api_key = cfg.get('api_key', '')
        api_base = cfg.get('api_base', 'https://api.vmcode.dev')

        if not api_key:
            console.print("[yellow]No API key set for bone. Use /key to set one.[/yellow]")
            console.print()
            return CommandResult(status="handled")

        # Fetch usage from proxy and render percentage bars
        status_code, usage = _call_proxy_api("GET", "/v1/usage", api_base, api_key=api_key)
        if status_code == 0 or usage is None:
            console.print("[red]Failed to fetch usage from bone.[/red]")
            console.print("[dim]Check your API key and network connection.[/dim]")
            console.print()
            return CommandResult(status="handled")

        plan_label = usage.get("plan", "unknown").capitalize()
        console.print(f"[bold #5F9EA0]Usage -- {plan_label} Plan[/bold #5F9EA0]")
        console.print()

        for period in ("daily", "weekly"):
            data = usage.get(period, {})
            pct = data.get("pct_used", 0)
            label = period.capitalize()
            filled = int(round(pct / 100 * 20))
            bar = "\u2588" * filled + "\u2591" * (20 - filled)
            reset_at = data.get("reset_at", "")
            if pct >= 90:
                indicator = "[bold red]![/bold red]"
            elif pct >= 70:
                indicator = "[bold yellow]~[/bold yellow]"
            else:
                indicator = "[bold green]+[/bold green]"
            reset_str = f"  [dim]resets {reset_at}[/dim]" if reset_at else ""
            console.print(f"  {indicator} [bold]{label:7s}[/bold]  {bar}  [bold]{pct:.1f}%[/bold]{reset_str}")

        console.print()
        return CommandResult(status="handled")

    # All other providers: show local session stats
    costs = config_manager.get_model_price(current_model)
    tracker = chat_manager.token_tracker

    # Display token counts
    console.print(f"[#5F9EA0]Session Token Usage ({current_model}):[/#5F9EA0]")
    console.print(f"  Input tokens:  {tracker.total_prompt_tokens:,}")
    console.print(f"  Output tokens: {tracker.total_completion_tokens:,}")
    console.print(f"  Total tokens:  {tracker.total_tokens:,}")

    # Display cache token breakdown when cache tokens were recorded.
    # Codex can report an explicit 0 cached_tokens value, which is still useful
    # confirmation that prompt-cache usage data is flowing through.
    show_cache = (
        tracker.total_cache_read_tokens > 0
        or tracker.total_cache_creation_tokens > 0
        or current_provider == "codex"
    )
    if show_cache:
        total_cached = tracker.total_cache_read_tokens + tracker.total_cache_creation_tokens
        cache_activity_read_pct = (
            tracker.total_cache_read_tokens
            / total_cached * 100
        ) if total_cached > 0 else 0
        cache_coverage_pct = (
            tracker.total_cache_read_tokens
            / tracker.total_prompt_tokens * 100
        ) if tracker.total_prompt_tokens > 0 else 0
        console.print()
        console.print(
            f"[#5F9EA0]Input Cache ({cache_coverage_pct:.0f}% input cached, "
            f"{cache_activity_read_pct:.0f}% cache reads):[/#5F9EA0]"
        )
        console.print(f"  Cache read:   {tracker.total_cache_read_tokens:,} tokens")
        console.print(f"  Cache write:  {tracker.total_cache_creation_tokens:,} tokens")
        if current_provider == "codex" and total_cached == 0:
            if tracker.last_cache_metrics_reported is False:
                keys = ", ".join(tracker.last_usage_keys) if tracker.last_usage_keys else "none"
                console.print("  [dim]Codex did not report any cache-token fields in the last usage payload.[/dim]")
                console.print(f"  [dim]Last usage keys: {keys}[/dim]")
            elif tracker.last_cache_metrics_reported is None:
                console.print("  [dim]No usage payload has been recorded yet for this Codex session.[/dim]")
    console.print()
    

    # Display costs — combined upstream-reported + estimated
    display_cost = tracker.get_display_cost(current_model)
    if display_cost > 0:
        console.print(f"[#5F9EA0]Session Cost ({current_model}):[/#5F9EA0]")
        console.print(f"  Total:  ${display_cost:.6f}")
        console.print()
        if tracker.has_actual_cost():
            console.print(f"[dim]Note: Includes ${tracker.total_actual_cost:.6f} provider-reported "
                          f"+ ${tracker.total_estimated_cost:.6f} locally estimated.[/dim]")
        else:
            console.print(f"[dim]Note: Cost estimated from token counts × static rates.[/dim]")
        console.print()
    else:
        if costs['in'] > 0 or costs['out'] > 0:
            console.print("  No cost data available (no tokens used yet).")
            console.print(f"[dim]Rates: ${costs['in']:.6f}/1M in, ${costs['out']:.6f}/1M out[/dim]")
            console.print()
        else:
            console.print(f"[yellow]Cost not configured for model '{current_model}'. Set with:[/yellow]")
            console.print(f"  [bold #5F9EA0]/usage[/bold #5F9EA0] in <cost>   - Set input token cost per 1M tokens")
            console.print(f"  [bold #5F9EA0]/usage[/bold #5F9EA0] out <cost>  - Set output token cost per 1M tokens")
            console.print(f"[dim]Example: [bold #5F9EA0]/usage[/bold #5F9EA0] in 2.50[/dim]")
            console.print()

    return CommandResult(status="handled")


def _handle_review(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle review command - run code review on git changes."""
    import subprocess
    import os
    import sys

    from tools.review_sub_agent import review_changes

    # Parse args: separate git diff flags from user intent
    # Format: /r [git-args] [-- intent description]
    # Examples:
    #   /r --staged
    #   /r I wanted to reduce the system prompt length
    #   /r --staged -- I was refactoring the auth module
    user_intent = None
    git_args = ""

    if args and args.strip():
        raw_args = args.strip()
        # Explicit delimiter: " -- " splits git args from intent
        if " -- " in raw_args:
            parts = raw_args.split(" -- ", 1)
            git_args = parts[0].strip()
            user_intent = parts[1].strip()
        else:
            # Heuristic: tokens starting with '-' are git flags, rest is intent
            tokens = raw_args.split()
            git_tokens = []
            intent_tokens = []
            in_intent = False
            for token in tokens:
                if in_intent or not token.startswith("-"):
                    in_intent = True
                    intent_tokens.append(token)
                else:
                    git_tokens.append(token)
            git_args = " ".join(git_tokens)
            if intent_tokens:
                user_intent = " ".join(intent_tokens)

    # Build git diff argument list (no shell=True to prevent command injection)
    git_argv = ["git", "diff"] + git_args.split()

    # Reject shell metacharacters as defense-in-depth
    import re
    dangerous = re.compile(r'[;&|`$(){}<>!]')
    for arg in git_argv[2:]:
        if dangerous.search(arg):
            console.print(f"[red]Rejected dangerous character in argument: {arg}[/red]")
            return CommandResult(status="handled")

    if user_intent:
        console.print(f"[#5F9EA0]Running: {' '.join(git_argv)}[/#5F9EA0]")
        console.print(f"[dim]Intent: {user_intent}[/dim]")
    else:
        console.print(f"[#5F9EA0]Running: {' '.join(git_argv)}[/#5F9EA0]")

    # Run git diff
    result = subprocess.run(
        git_argv,
        shell=False,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        console.print(f"[red]git diff failed:[/red]")
        console.print(f"[dim]{result.stderr.strip()}[/dim]")
        return CommandResult(status="handled")

    diff_output = result.stdout.strip()
    if not diff_output:
        console.print("[yellow]No changes to review.[/yellow]")
        return CommandResult(status="handled")

    # Count changed files for summary
    file_count = diff_output.count("diff --git ")
    console.print(f"[dim]Reviewing {file_count} changed file(s)...[/dim]")
    console.print()

    # Compute paths from the active working directory
    from pathlib import Path
    from utils.paths import RG_EXE_PATH as _RG_EXE_PATH
    repo_root = Path.cwd().resolve()
    rg_exe_path = str(_RG_EXE_PATH)

    # Create a live panel for the review sub-agent
    panel = SubAgentPanel("Reviewing git diff", console)

    # Run the review
    review_result = review_changes(
        diff_output=diff_output,
        repo_root=repo_root,
        rg_exe_path=rg_exe_path,
        console=console,
        chat_manager=chat_manager,
        panel_updater=panel,
        user_intent=user_intent,
    )

    display_text = review_result["display"]
    history_text = review_result["history"]

    # Display clean result as rendered Markdown (no injected file contents)
    if display_text:
        console.print()
        md = Markdown(left_align_headings(display_text), code_theme=MonokaiDarkBGStyle, justify="left")
        console.print(md)
        console.print()

    # Inject review (with file contents) into chat history for follow-up context
    if history_text:
        review_cmd = "/review"
        if user_intent:
            review_cmd += f"\n\nUser intent: {user_intent}"
        chat_manager.messages.append({
            "role": "user",
            "content": review_cmd
        })
        chat_manager.messages.append({
            "role": "assistant",
            "content": f"Here is the code review of the current git diff:\n\n{history_text}"
        })

        # Update context token tracker so compaction timing stays accurate
        injected_tokens = chat_manager.token_tracker.estimate_tokens(
            f"{review_cmd}\n\n{history_text}"
        )
        chat_manager.token_tracker.current_context_tokens += injected_tokens

    return CommandResult(status="handled")


# ============================================
# Shared proxy API helper
# ============================================

def _call_proxy_api(
    method: str,
    path: str,
    api_base: str,
    body: dict | None = None,
    api_key: str | None = None,
    timeout: int = 10,
) -> tuple[int, dict | None]:
    """Call a bone-proxy API endpoint.

    Returns (status_code, parsed_json_or_None).
    Returns (0, None) on network/parse failures.
    """
    # Validate endpoint uses HTTPS (or localhost HTTP)
    full_url = f"{api_base.rstrip('/')}{path}"
    valid, err = validate_api_url(full_url)
    if not valid:
        logger.warning("Proxy API call rejected: %s", err)
        return (0, None)

    # Enforce TLS regardless of global settings
    ssl_ctx = ssl.create_default_context()

    try:
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(full_url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")

        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            return (resp.status, json.loads(resp.read().decode()))
    except urllib.error.HTTPError as e:
        try:
            return (e.code, json.loads(e.read().decode()))
        except Exception:
            return (e.code, None)
    except Exception as e:
        logger.debug("Proxy API call failed: %s", e)
        return (0, None)


def _get_proxy_config(chat_manager):
    """Get bone api_key and api_base from current config.

    Returns (api_key, api_base) tuple. api_key may be empty string.
    """
    cfg = config.get_provider_config("bone")
    api_key = cfg.get("api_key", "")
    api_base = cfg.get("api_base", "https://api.vmcode.dev")
    return api_key, api_base


def _require_proxy_provider(chat_manager, console):
    """Check that bone is the current provider.

    Returns True if on bone, prints error and returns False otherwise.
    """
    current_provider = getattr(chat_manager.client, "provider", "unknown")
    if current_provider != "bone":
        console.print(
            "[yellow]This command requires the bone provider.[/yellow]"
        )
        console.print("[dim]Run [bold #5F9EA0]/provider bone[/bold #5F9EA0] first.[/dim]")
        console.print()
        return False
    return True


# ============================================
# Account command handlers
# ============================================

def _handle_plan(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle /plan — show available plans."""
    _, api_base = _get_proxy_config(chat_manager)

    # Try the API first
    status, data = _call_proxy_api("GET", "/v1/billing/plans", api_base)

    if status == 200 and data and "plans" in data:
        plans = data["plans"]
    else:
        # Fallback to hardcoded defaults
        plans = [
            {"id": "free", "name": "Free", "price": 0, "tokens": 0, "rate_limit": 0},
            {"id": "lite", "name": "Lite", "price": 10, "tokens": 2_000_000, "rate_limit": 60},
            {"id": "pro", "name": "Pro", "price": 50, "tokens": 15_000_000, "rate_limit": 300},
        ]

    # Determine current plan
    current_provider = getattr(chat_manager.client, "provider", "unknown")
    current_plan = None
    if current_provider == "bone":
        api_key, _ = _get_proxy_config(chat_manager)
        if api_key:
            acct_status, acct_data = _call_proxy_api("GET", "/v1/auth/account", api_base, api_key=api_key)
            if acct_status == 200 and acct_data:
                current_plan = acct_data.get("plan")

    table = Table("Plan", "Price", "Rate Limit (req/min)", title="Available Plans", box=box.SIMPLE_HEAD)
    for plan in plans:
        is_current = current_plan and plan["id"] == current_plan
        name = f"[bold green]{plan['name']} (current)[/bold green]" if is_current else plan["name"]
        if plan["id"] == "free":
            price = "Free model only"
            rate = "N/A"
        else:
            price = f"${plan.get('price', 0)}/mo" if plan.get("price", 0) > 0 else "Free"
            rate = str(plan["rate_limit"]) if plan.get("rate_limit") is not None else "N/A"
        table.add_row(name, price, rate)

    console.print(table)
    console.print("[dim]Upgrade: [bold #5F9EA0]/upgrade pro[/bold #5F9EA0]  |  Manage: [bold #5F9EA0]/account[/bold #5F9EA0][/dim]")
    console.print()
    return CommandResult(status="handled")


def _handle_signup(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle /signup <email> — create account and switch to bone."""
    if not args or not args.strip():
        console.print("[red]Usage: /signup <email>[/red]")
        console.print("[dim]Creates a bone-agent account and generates an API key.[/dim]")
        console.print()
        return CommandResult(status="handled")

    email = args.strip()

    # Basic client-side email validation
    if "@" not in email or "." not in email.split("@")[-1]:
        console.print("[red]Invalid email address.[/red]")
        console.print()
        return CommandResult(status="handled")

    _, api_base = _get_proxy_config(chat_manager)
    console.print(f"[#5F9EA0]Creating account for {email}...[/#5F9EA0]")

    status, data = _call_proxy_api("POST", "/v1/auth/signup", api_base, body={"email": email})

    if status == 409:
        console.print("[yellow]Account already exists for that email.[/yellow]")
        console.print("[dim]Use [bold #5F9EA0]/login {email}[/bold #5F9EA0] to log in on this device.[/dim]")
        console.print()
        return CommandResult(status="handled")

    if status != 201 and status != 200:
        detail = (data or {}).get("detail", "Unknown error") if data else "Network error"
        console.print(f"[red]Signup failed: {detail}[/red]")
        console.print()
        return CommandResult(status="handled")

    if not data or "api_key" not in data:
        console.print("[red]Signup failed: unexpected response from server.[/red]")
        console.print()
        return CommandResult(status="handled")

    api_key = data["api_key"]

    # Display the API key prominently
    console.print()
    console.print("[bold green]Account created successfully![/bold green]")
    console.print("[dim]Check your inbox for a verification email. Use [bold #5F9EA0]/resend[/bold #5F9EA0] if it doesn't arrive.[/dim]")
    console.print()
    console.print("[bold #5F9EA0]Your API key (save this — it won't be shown again):[/bold #5F9EA0]")
    console.print(f"[bold white on grey23]  {api_key}  [/bold white on grey23]")
    console.print()

    # Save backup to ~/.bone/api_key.txt
    try:
        key_path = Path.home() / ".bone" / "api_key.txt"
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text(api_key)
        key_path.chmod(0o600)
        console.print(f"[dim]Key backed up to {key_path}[/dim]")
    except Exception as e:
        console.print(f"[yellow]Could not save key backup: {e}[/yellow]")

    # Persist API key to config (always succeeds or warns — never blocks)
    try:
        config_manager.set_api_key("bone", api_key)
    except Exception as e:
        console.print(f"[yellow]Could not save API key to config: {e}[/yellow]")
        console.print("[dim]Use [bold #5F9EA0]/key {api_key}[/bold #5F9EA0] to set it manually.[/dim]")

    # Switch to bone provider (best-effort)
    try:
        config_manager.set_provider("bone")
        chat_manager.reload_config()
        chat_manager.switch_provider("bone")
        console.print("[green]Switched to bone provider.[/green]")
    except Exception as e:
        console.print(f"[yellow]Could not auto-switch to bone: {e}[/yellow]")
        console.print("[dim]Run [bold #5F9EA0]/provider bone[/bold #5F9EA0] to switch manually.[/dim]")

    console.print()
    return CommandResult(status="handled")


def _handle_account(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle /account — show account info."""
    if not _require_proxy_provider(chat_manager, console):
        return CommandResult(status="handled")

    api_key, api_base = _get_proxy_config(chat_manager)
    if not api_key:
        console.print("[yellow]No API key set for bone. Use /key to set one.[/yellow]")
        console.print()
        return CommandResult(status="handled")

    console.print("[#5F9EA0]Fetching account info...[/#5F9EA0]")
    status, data = _call_proxy_api("GET", "/v1/auth/account", api_base, api_key=api_key)

    if status != 200 or not data:
        detail = (data or {}).get("detail", "Unknown error") if data else "Network error"
        console.print(f"[red]Failed to fetch account: {detail}[/red]")
        console.print()
        return CommandResult(status="handled")

    console.print()
    console.print(f"[bold #5F9EA0]Account:[/bold #5F9EA0]  {data.get('email', 'N/A')}")
    plan = data.get("plan", "lite").capitalize()
    sub_status = data.get("subscription_status", "none")
    console.print(f"[bold #5F9EA0]Plan:[/bold #5F9EA0]      {plan}")

    if sub_status and sub_status != "none":
        console.print(f"[bold #5F9EA0]Status:[/bold #5F9EA0]    {sub_status}")
        period_end = data.get("current_period_end")
        if period_end:
            console.print(f"[bold #5F9EA0]Renews:[/bold #5F9EA0]    {period_end}")
    else:
        console.print("[dim]No active subscription[/dim]")

    prefix = data.get("api_key_prefix")
    if prefix:
        console.print(f"[bold #5F9EA0]API key:[/bold #5F9EA0]   {prefix}...")
    key_count = len(data.get("keys", []))
    console.print(f"[bold #5F9EA0]Keys:[/bold #5F9EA0]      {key_count}")
    console.print()
    console.print("[dim]Manage subscription: [bold #5F9EA0]/upgrade[/bold #5F9EA0] or [bold #5F9EA0]/manage[/bold #5F9EA0][/dim]")
    console.print()
    return CommandResult(status="handled")


def _send_reset_key_email(console, api_base, email):
    """Shared logic for sending a new API key via email.

    Used by both /login (path 2: user lost key) and /reset-key.
    Returns CommandResult.
    """
    console.print(f"[#5F9EA0]Sending new API key to {email}...[/#5F9EA0]")
    console.print("[dim]This will create a new key and email it to you. Old keys remain valid.[/dim]")
    console.print()

    from rich.prompt import Confirm
    if not Confirm.ask("Send a new API key to this email?", default=False):
        console.print("[dim]Cancelled.[/dim]")
        console.print()
        return CommandResult(status="handled")

    status, data = _call_proxy_api("POST", "/v1/auth/reset-key", api_base, body={"email": email})

    if status == 429:
        detail = (data or {}).get("detail", "Too many requests.") if data else "Too many requests."
        console.print(f"[yellow]{detail}[/yellow]")
        console.print()
        return CommandResult(status="handled")

    if status != 200 and status != 201:
        detail = (data or {}).get("detail", "Unknown error") if data else "Network error"
        console.print(f"[red]Failed to send: {detail}[/red]")
        console.print()
        return CommandResult(status="handled")

    message = (data or {}).get("message", "Check your email for the new API key.")
    console.print(f"[green]{message}[/green]")
    console.print("[dim]Once you receive the key, run: [bold #5F9EA0]/key <your-new-key>[/bold #5F9EA0][/dim]")
    console.print()
    return CommandResult(status="handled")


def _handle_login(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle /login <email> — log in to an existing bone-agent account on this device.

    Two paths:
    - User has their API key: validate it, save to config, switch provider.
    - User lost their key: email a new one via /reset-key endpoint.
    """
    if not args or not args.strip():
        console.print("[red]Usage: /login <email>[/red]")
        console.print("[dim]Log in to an existing bone-agent account on this device.[/dim]")
        console.print()
        return CommandResult(status="handled")

    email = args.strip()

    # Basic client-side email validation
    if "@" not in email or "." not in email.split("@")[-1]:
        console.print("[red]Invalid email address.[/red]")
        console.print()
        return CommandResult(status="handled")

    if not _require_proxy_provider(chat_manager, console):
        return CommandResult(status="handled")

    # Check if already logged in to a different account
    api_key, api_base = _get_proxy_config(chat_manager)
    if api_key:
        try:
            acct_status, acct_data = _call_proxy_api("GET", "/v1/auth/account", api_base, api_key=api_key)
            if acct_status == 200 and acct_data:
                current_email = acct_data.get("email", "")
                if current_email and current_email.lower() != email.lower():
                    from rich.prompt import Confirm
                    console.print(f"[yellow]Already logged in as {current_email}[/yellow]")
                    if not Confirm.ask(f"Switch to {email}?", default=False):
                        console.print("[dim]Cancelled.[/dim]")
                        console.print()
                        return CommandResult(status="handled")
        except Exception:
            pass  # If we can't check, just proceed

    console.print()
    console.print(f"[bold #5F9EA0]bone-agent Login[/bold #5F9EA0]")
    console.print(f"[dim]Logging in as {email}[/dim]")
    console.print()

    from rich.prompt import Confirm, Prompt

    if Confirm.ask("Do you have your API key?", default=True):
        # Path 1: user has their key — validate and save
        raw_key = Prompt.ask("API key")

        if not raw_key.strip():
            console.print("[yellow]No key entered. Aborted.[/yellow]")
            console.print()
            return CommandResult(status="handled")

        raw_key = raw_key.strip()

        console.print("[#5F9EA0]Validating API key...[/#5F9EA0]")
        status, data = _call_proxy_api("GET", "/v1/auth/account", api_base, api_key=raw_key)

        if status == 200 and data and data.get("email", "").lower() == email.lower():
            # Valid key — save and switch
            try:
                config_manager.set_api_key("bone", raw_key)
            except Exception as e:
                console.print(f"[yellow]Could not save API key to config: {e}[/yellow]")
                console.print(f"[dim]Use [bold #5F9EA0]/key {raw_key}[/bold #5F9EA0] to set it manually.[/dim]")

            try:
                config_manager.set_provider("bone")
                chat_manager.reload_config()
                chat_manager.switch_provider("bone")
                console.print("[green]Switched to bone provider.[/green]")
            except Exception as e:
                console.print(f"[yellow]Could not auto-switch to bone: {e}[/yellow]")
                console.print("[dim]Run [bold #5F9EA0]/provider bone[/bold #5F9EA0] to switch manually.[/dim]")

            plan = data.get("plan", "free")
            verified = "yes" if data.get("verified") else "no"
            console.print(f"[green]Logged in as {email}[/green] (plan: {plan}, verified: {verified})")
            console.print()
            return CommandResult(status="handled")

        if status in (401, 403):
            console.print("[red]Invalid API key.[/red]")
            console.print("[dim]Double-check your key and try again, or say 'no' to get a new one emailed.[/dim]")
        else:
            detail = (data or {}).get("detail", "Unknown error") if data else "Network error"
            console.print(f"[red]Validation failed: {detail}[/red]")
        console.print()
        return CommandResult(status="handled")

    # Path 2: user lost their key — email a new one
    return _send_reset_key_email(console, api_base, email)



def _handle_resend(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle /resend [email] — resend verification email.

    If no email is given, fetches it from the account endpoint.
    """
    if not _require_proxy_provider(chat_manager, console):
        return CommandResult(status="handled")

    api_key, api_base = _get_proxy_config(chat_manager)
    if not api_key:
        console.print("[yellow]No API key set for bone. Use /key to set one.[/yellow]")
        console.print()
        return CommandResult(status="handled")

    # Resolve email: use arg, or fetch from account
    email = args.strip() if args and args.strip() else None
    if not email:
        acct_status, acct_data = _call_proxy_api("GET", "/v1/auth/account", api_base, api_key=api_key)
        if acct_status == 200 and acct_data:
            email = acct_data.get("email")
            if acct_data.get("verified"):
                console.print("[green]Email is already verified.[/green]")
                console.print()
                return CommandResult(status="handled")

    if not email:
        console.print("[red]Could not determine your email. Usage: /resend <email>[/red]")
        console.print()
        return CommandResult(status="handled")

    console.print(f"[#5F9EA0]Sending verification email to {email}...[/#5F9EA0]")
    status, data = _call_proxy_api("POST", "/v1/auth/resend", api_base, body={"email": email})

    if status == 429:
        detail = (data or {}).get("detail", "Too many requests.") if data else "Too many requests."
        console.print(f"[yellow]{detail}[/yellow]")
        console.print()
        return CommandResult(status="handled")

    if status != 200 and status != 201:
        detail = (data or {}).get("detail", "Unknown error") if data else "Network error"
        console.print(f"[red]Failed to send: {detail}[/red]")
        console.print()
        return CommandResult(status="handled")

    message = (data or {}).get("message", "Verification email sent.")
    console.print(f"[green]{message}[/green]")
    console.print()
    return CommandResult(status="handled")


def _handle_reset_key(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle /reset-key [email] — request a new API key via email.

    If no email is given, fetches it from the account endpoint (requires API key).
    If email is given, works without an API key (for users who lost everything).
    """
    if not _require_proxy_provider(chat_manager, console):
        return CommandResult(status="handled")

    api_key, api_base = _get_proxy_config(chat_manager)

    # Resolve email: use arg, or fetch from account
    email = args.strip() if args and args.strip() else None
    if not email and api_key:
        acct_status, acct_data = _call_proxy_api("GET", "/v1/auth/account", api_base, api_key=api_key)
        if acct_status == 200 and acct_data:
            email = acct_data.get("email")

    if not email:
        console.print("[red]Could not determine your email. Usage: /reset-key <email>[/red]")
        console.print()
        return CommandResult(status="handled")

    return _send_reset_key_email(console, api_base, email)


def _handle_manage(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle /manage — open Stripe Customer Portal for subscription management."""
    if not _require_proxy_provider(chat_manager, console):
        return CommandResult(status="handled")

    api_key, api_base = _get_proxy_config(chat_manager)
    if not api_key:
        console.print("[yellow]No API key set for bone. Use /key to set one.[/yellow]")
        console.print()
        return CommandResult(status="handled")

    console.print("[#5F9EA0]Opening billing portal...[/#5F9EA0]")
    status, data = _call_proxy_api(
        "POST", "/v1/billing/portal", api_base,
        body={"return_url": "https://vmcode.dev"},
        api_key=api_key,
    )

    if status == 400:
        detail = (data or {}).get("detail", "No subscription found.") if data else "No subscription found."
        console.print(f"[yellow]{detail}[/yellow]")
        console.print("[dim]Subscribe to a plan first with [bold #5F9EA0]/upgrade[/bold #5F9EA0].[/dim]")
        console.print()
        return CommandResult(status="handled")

    if status != 200 or not data or "url" not in data:
        detail = (data or {}).get("detail", "Unknown error") if data else "Network error"
        console.print(f"[red]Failed to open billing portal: {detail}[/red]")
        console.print()
        return CommandResult(status="handled")

    url = data["url"]

    try:
        import webbrowser
        webbrowser.open(url)
        console.print("[green]Opened in browser[/green]")
    except Exception:
        pass

    console.print()
    console.print("[#5F9EA0]Or copy this link:[/#5F9EA0]")
    console.print(f"  [bold]{url}[/bold]")
    console.print()
    return CommandResult(status="handled")


def _handle_upgrade(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle /upgrade — show plan selector, then open checkout or billing portal."""
    if not _require_proxy_provider(chat_manager, console):
        return CommandResult(status="handled")

    api_key, api_base = _get_proxy_config(chat_manager)
    if not api_key:
        console.print("[yellow]No API key set for bone. Use /key to set one.[/yellow]")
        console.print()
        return CommandResult(status="handled")

    # Check current plan for showing the current selection
    current_plan = "free"
    acct_status, acct_data = _call_proxy_api("GET", "/v1/auth/account", api_base, api_key=api_key)
    if acct_status == 200 and acct_data:
        current_plan = acct_data.get("plan", "free")

    # Get available plans from API
    status, data = _call_proxy_api("GET", "/v1/billing/plans", api_base)
    if status == 200 and data and "plans" in data:
        plans = data["plans"]
    else:
        # Fallback to hardcoded defaults
        plans = [
            {"id": "free", "name": "Free", "price": 0, "tokens": 0, "rate_limit": 0},
            {"id": "lite", "name": "Lite", "price": 10, "tokens": 2_000_000, "rate_limit": 60},
            {"id": "pro", "name": "Pro", "price": 50, "tokens": 15_000_000, "rate_limit": 300},
        ]

    # Only show plans the user can upgrade to (current plan excluded)
    # Tier ordering: free < lite < pro
    _TIER_ORDER = {"free": 0, "lite": 1, "pro": 2}
    current_tier = _TIER_ORDER.get(current_plan, 0)

    upgradeable_plans = [
        p for p in plans
        if _TIER_ORDER.get(p["id"], 0) > current_tier
    ]

    if not upgradeable_plans:
        # Pro user — no upgrades available
        console.print()
        console.print(f"[bold green]You're on the {current_plan.capitalize()} plan — the highest tier.[/bold green]")
        console.print("[dim]Use [bold #5F9EA0]/manage[/bold #5F9EA0] to cancel or change your subscription.[/dim]")
        console.print()
        return CommandResult(status="handled")

    # Build plan options from upgradeable plans only
    plan_options = []
    for plan in upgradeable_plans:
        price_desc = f"${plan['price']}/mo" if plan.get("price", 0) > 0 else "Free"
        plan_options.append({
            "value": plan["id"],
            "text": plan["name"],
            "description": price_desc,
        })

    # Default selection to the first upgradeable plan
    first_upgrade = upgradeable_plans[0]["id"]

    # Show plan selector — title includes current plan
    selector = SettingSelector(
        categories=[
            SettingCategory(
                title="Select Plan",
                settings=[
                    SettingOption(
                        key="plan",
                        text=f"Select a plan  (current: {current_plan.capitalize()}):",
                        value=first_upgrade,
                        input_type="options",
                        options=plan_options,
                    )
                ]
            )
        ],
        title="Upgrade Your Plan",
        show_save=False,
    )

    result = selector.run()

    if result is None:
        console.print("[dim]Cancelled.[/dim]")
        console.print()
        return CommandResult(status="handled")

    target = result.get("plan", first_upgrade)

    # Upgrade: open Stripe Checkout
    console.print(f"[#5F9EA0]Opening checkout for {target.capitalize()}...[/#5F9EA0]")

    status, data = _call_proxy_api(
        "POST", "/v1/billing/checkout", api_base,
        body={
            "plan": target,
            "success_url": "https://vmcode.dev",
            "cancel_url": "https://vmcode.dev",
        },
        api_key=api_key,
    )
    action = "create checkout session"

    if status == 200 and data and "url" in data:
        url = data["url"]
    else:
        detail = (data or {}).get("detail", "Unknown error") if data else "Network error"
        console.print(f"[red]Failed to {action}: {detail}[/red]")
        console.print()
        return CommandResult(status="handled")

    # Open in browser
    try:
        import webbrowser
        webbrowser.open(url)
        console.print("[green]Opened in browser[/green]")
    except Exception:
        pass

    console.print()
    console.print("[#5F9EA0]Or copy this link:[/#5F9EA0]")
    console.print(f"  [bold]{url}[/bold]")
    console.print()
    return CommandResult(status="handled")


def _handle_rotate_key(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle /rotate-key — invalidate current API key and generate a new one."""
    if not _require_proxy_provider(chat_manager, console):
        return CommandResult(status="handled")

    api_key, api_base = _get_proxy_config(chat_manager)
    if not api_key:
        console.print("[yellow]No API key set for bone. Use /key to set one.[/yellow]")
        console.print()
        return CommandResult(status="handled")

    # Warn user
    console.print("[bold yellow]This will invalidate your current API key and generate a new one.[/bold yellow]")
    console.print("[dim]Make sure you can save the new key before proceeding.[/dim]")
    console.print()

    from rich.prompt import Confirm
    if not Confirm.ask("Rotate your API key?", default=False):
        console.print("[dim]Cancelled.[/dim]")
        console.print()
        return CommandResult(status="handled")

    console.print("[#5F9EA0]Rotating API key...[/#5F9EA0]")
    status, data = _call_proxy_api(
        "POST", "/v1/auth/rotate-key", api_base,
        body={},
        api_key=api_key,
    )

    if status != 200 or not data or "api_key" not in data:
        detail = (data or {}).get("detail", "Unknown error") if data else "Network error"
        console.print(f"[red]Failed to rotate key: {detail}[/red]")
        console.print()
        return CommandResult(status="handled")

    new_key = data["api_key"]

    # Display new key
    console.print()
    console.print("[bold green]API key rotated successfully.[/bold green]")
    console.print("[bold red]Your old key is no longer valid.[/bold red]")
    console.print()
    console.print("[bold #5F9EA0]Your new API key (save this — it won't be shown again):[/bold #5F9EA0]")
    console.print(f"[bold white on grey23]  {new_key}  [/bold white on grey23]")
    console.print()

    # Save to config
    try:
        config_manager.set_api_key("bone", new_key)
        console.print("[green]New key saved to config.[/green]")
    except Exception as e:
        console.print(f"[yellow]Could not save to config: {e}[/yellow]")
        console.print(f"[dim]Use [bold #5F9EA0]/key {new_key}[/bold #5F9EA0] to set it manually.[/dim]")

    # Backup to file
    try:
        key_path = Path.home() / ".bone" / "api_key.txt"
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text(new_key)
        key_path.chmod(0o600)
        console.print(f"[dim]Key backed up to {key_path}[/dim]")
    except Exception:
        pass

    console.print()
    return CommandResult(status="handled")


def _persist_obsidian_config(console, **kwargs):
    """Persist Obsidian settings to config file.

    Args:
        console: Rich console for output
        **kwargs: OBSIDIAN_SETTINGS fields to persist
    """
    try:
        config_data = config_manager.load(force_reload=True)
        if "OBSIDIAN_SETTINGS" not in config_data:
            config_data["OBSIDIAN_SETTINGS"] = {}
        config_data["OBSIDIAN_SETTINGS"].update(kwargs)
        config_manager.save(config_data)
    except Exception as e:
        console.print(f"[yellow]Saved to session but could not persist to config: {e}[/yellow]")
        console.print("[dim]Settings will reset on restart.[/dim]")


def _apply_obsidian_changes(chat_manager, console, obsidian_settings, changes):
    """Apply Obsidian setting changes, register/unregister tools, persist config.

    Args:
        chat_manager: ChatManager instance
        console: Rich console for output
        obsidian_settings: ObsidianSettings instance
        changes: dict of {key: new_value} from SettingSelector

    Returns:
        list of change description strings
    """
    change_lines = []
    was_active = obsidian_settings.is_active()

    for key, value in changes.items():
        if key == "vault_path":
            old_path = obsidian_settings.vault_path
            new_path = value.strip() if value else ""
            if new_path and new_path != old_path:
                # Validate path
                vault_path = Path(new_path).resolve()
                if not vault_path.is_dir():
                    console.print(f"[red]Not a directory: {vault_path}[/red]")
                    continue
                if not (vault_path / ".obsidian").is_dir():
                    console.print(f"[red]No .obsidian/ directory found in: {vault_path}[/red]")
                    console.print("[dim]Make sure this is a valid Obsidian vault.[/dim]")
                    continue
                obsidian_settings.update(vault_path=str(vault_path))
                change_lines.append(f"  Vault path: {vault_path}")
            elif not new_path and old_path:
                obsidian_settings.update(vault_path="")
                change_lines.append("  Vault path: (cleared)")
        elif key == "enabled":
            obsidian_settings.update(enabled=value)
            state = "enabled" if value else "disabled"
            change_lines.append(f"  Integration: {state}")
        elif key == "exclude_folders":
            obsidian_settings.update(exclude_folders=value)
            change_lines.append(f"  Exclude folders: {value}")
        elif key == "project_base":
            obsidian_settings.update(project_base=value.strip() if value else "Dev")
            change_lines.append(f"  Project base: {obsidian_settings.project_base}")

    # Note: vault session is initialized lazily by init_session() in agentic.py
    # No tool registration needed — vault utilities are used internally

    # Persist all settings to config
    if changes:
        _persist_obsidian_config(
            console,
            vault_path=obsidian_settings.vault_path,
            enabled=obsidian_settings.enabled,
            exclude_folders=obsidian_settings.exclude_folders,
            project_base=obsidian_settings.project_base,
        )

    return change_lines


def _handle_obsidian(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle /obsidian command — manage vault integration.

    No args: Launch interactive SettingSelector UI (same UX as /config).
    Subcommands: set <path>, enable, disable, status — quick shortcuts.
    """
    from ui.setting_selector import SettingOption, SettingCategory, SettingSelector
    from utils.settings import obsidian_settings

    # Text subcommands (quick shortcuts)
    if args:
        args_clean = args.strip()

        if args_clean.lower() == "status":
            active = obsidian_settings.is_active()
            configured = obsidian_settings.is_configured()
            if active:
                console.print("[green]Obsidian integration: ACTIVE[/green]")
            elif configured:
                console.print("[yellow]Obsidian integration: ENABLED but vault invalid[/yellow]")
            else:
                console.print("[dim]Obsidian integration: DISABLED[/dim]")
            console.print(f"  Vault path: {obsidian_settings.vault_path or '(not set)'}")
            console.print(f"  Enabled: {obsidian_settings.enabled}")
            console.print(f"  Exclude folders: {obsidian_settings.exclude_folders}")
            console.print(f"  Project base: {obsidian_settings.project_base}")
            console.print()
            console.print("[dim]Run [bold #5F9EA0]/obsidian[/bold #5F9EA0] (no args) for interactive settings.[/dim]")
            return CommandResult(status="handled")

        if args_clean.lower().startswith("set "):
            path = args_clean[4:].strip().strip('"').strip("'")
            if not path:
                console.print("[red]Usage: [bold #5F9EA0]/obsidian set /path/to/your/vault[/bold #5F9EA0]")
                return CommandResult(status="handled")
            vault_path = Path(path).resolve()
            if not vault_path.is_dir():
                console.print(f"[red]Not a directory: {vault_path}[/red]")
                return CommandResult(status="handled")
            if not (vault_path / ".obsidian").is_dir():
                console.print(f"[red]No .obsidian/ directory found in: {vault_path}[/red]")
                return CommandResult(status="handled")
            changes = {"vault_path": str(vault_path), "enabled": True}
            change_lines = _apply_obsidian_changes(chat_manager, console, obsidian_settings, changes)
            console.print(f"[green]Obsidian vault set:[/green]")
            for line in change_lines:
                console.print(line)
            return CommandResult(status="handled")

        if args_clean.lower() == "enable":
            if not obsidian_settings.vault_path:
                console.print("[red]No vault path set. Use [bold #5F9EA0]/obsidian set <path>[/bold #5F9EA0] first.[/red]")
                return CommandResult(status="handled")
            changes = _apply_obsidian_changes(chat_manager, console, obsidian_settings, {"enabled": True})
            console.print("[green]Obsidian integration enabled.[/green]")
            return CommandResult(status="handled")

        if args_clean.lower() == "disable":
            _apply_obsidian_changes(chat_manager, console, obsidian_settings, {"enabled": False})
            console.print("[yellow]Obsidian integration disabled. Tools unregistered.[/yellow]")
            return CommandResult(status="handled")

        if args_clean.lower() == "init":
            return _handle_obsidian_init(console, obsidian_settings)

        console.print(f"[red]Unknown subcommand: {args}[/red]")
        console.print("Usage: [bold #5F9EA0]/obsidian[/bold #5F9EA0] [set <path> | enable | disable | status | init]")
        return CommandResult(status="handled")

    # No args — launch interactive SettingSelector UI
    vault_settings = [
        SettingOption(
            key="vault_path", text="Vault Path",
            value=obsidian_settings.vault_path or "",
            input_type="text",
            description="Absolute path to your Obsidian vault (.obsidian/ must exist)",
        ),
        SettingOption(
            key="enabled", text="Enable Integration",
            value=obsidian_settings.enabled,
            input_type="boolean",
            on_text="ON", off_text="OFF",
        ),
    ]

    behavior_settings = [
        SettingOption(
            key="exclude_folders", text="Exclude Folders",
            value=obsidian_settings.exclude_folders,
            input_type="text",
            description="Comma-separated folder names to skip during vault scans",
        ),
        SettingOption(
            key="project_base", text="Project Base",
            value=obsidian_settings.project_base,
            input_type="text",
            description="Base folder within vault for project notes (default: Dev)",
        ),
    ]

    categories = [
        SettingCategory(title="Vault", settings=vault_settings),
        SettingCategory(title="Behavior", settings=behavior_settings),
    ]

    selector = SettingSelector(
        categories=categories,
        title="Obsidian Integration",
    )

    changes = selector.run()

    if changes is None:
        console.print("[dim]Cancelled.[/dim]")
        return CommandResult(status="handled")

    if not changes:
        console.print("[dim]No changes made.[/dim]")
        return CommandResult(status="handled")

    change_lines = _apply_obsidian_changes(chat_manager, console, obsidian_settings, changes)

    if change_lines:
        # Show active status after changes
        is_active = obsidian_settings.is_active()
        status_label = "[green]ACTIVE[/green]" if is_active else "[dim]inactive[/dim]"
        console.print(f"[green]Obsidian settings updated:[/green] ({status_label})")
        for line in change_lines:
            console.print(line)
    else:
        console.print("[dim]No changes applied.[/dim]")

    return CommandResult(status="handled")


def _persist_tool_visibility(console):
    """Persist tool and skill visibility state to config file.

    Returns True on success, False on failure.
    """
    try:
        cfg_data = config_manager.load(force_reload=True)
        if "TOOL_SETTINGS" not in cfg_data:
            cfg_data["TOOL_SETTINGS"] = {}
        cfg_data["TOOL_SETTINGS"]["disabled_tools"] = list(tool_settings.disabled_tools)
        cfg_data["TOOL_SETTINGS"]["hidden_skills"] = list(tool_settings.hidden_skills)
        config_manager.save(cfg_data)
        return True
    except Exception as e:
        console.print(f"[yellow]Could not persist to config: {e}[/yellow]")
        return False


def _handle_tools(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle /tools command — manage tool availability and skill discovery visibility.

    No args: Launch interactive SettingSelector with tools/plugins grouped by category
    and a skills section for discovery visibility.
    Subcommands:
      list — show all tools, plugins, and skills with status
      enable <name> — enable a core tool or plugin
      disable <name> — disable a core tool or plugin
      show-skill <name> — make a skill visible in discovery surfaces
      hide-skill <name> — hide a skill from discovery surfaces
      enable-group <key> — enable all tools in a group (e.g. file_ops, task_mgmt)
      disable-group <key> — disable all tools in a group
    """
    from core.skills import iter_skill_summaries, validate_skill_name
    from ui.setting_selector import SettingOption, SettingCategory, SettingSelector
    from tools.helpers.base import ToolRegistry, TOOL_GROUPS
    from tools.helpers.plugin_manifest import plugin_manifest

    # Text subcommands
    if args:
        args_clean = args.strip()

        if args_clean.lower() in ("list", "status"):
            all_tools = sorted(ToolRegistry._tools.values(), key=lambda t: t.name)
            plugin_defs = sorted(plugin_manifest.get_all(), key=lambda t: t.name)
            skills = sorted(iter_skill_summaries(), key=lambda s: s.name)
            disabled = ToolRegistry.get_disabled()
            hidden_skills = set(tool_settings.hidden_skills)
            disabled_tools = {t.name for t in all_tools if t.name in disabled}
            console.print(
                f"[bold #5F9EA0]Tools: {len(all_tools) - len(disabled_tools)} enabled, {len(disabled_tools)} disabled[/bold #5F9EA0]"
            )
            console.print(
                f"[bold #5F9EA0]User plugins: {sum(1 for p in plugin_defs if p.name not in disabled)} enabled, {sum(1 for p in plugin_defs if p.name in disabled)} disabled[/bold #5F9EA0]"
            )
            console.print(
                f"[bold #5F9EA0]Skills: {len(skills) - len(hidden_skills)} visible, {len(hidden_skills)} hidden[/bold #5F9EA0]"
            )
            console.print()

            tool_to_group = {}
            for gkey, gdef in TOOL_GROUPS.items():
                for tname in gdef["tools"]:
                    tool_to_group.setdefault(tname, []).append(gdef["label"])

            current_group = None
            for t in all_tools:
                groups = tool_to_group.get(t.name, [])
                group_label = groups[0] if groups else "Other"
                is_off = t.name in disabled
                if group_label != current_group:
                    current_group = group_label
                    console.print(f"  [bold]{group_label}[/bold]")
                status = "[red]off[/red]" if is_off else "[green]on[/green] "
                console.print(f"    {status} {t.name}")

            console.print()
            console.print("  [bold]User plugins[/bold]")
            for plugin in plugin_defs:
                status = "[red]off[/red]" if plugin.name in disabled else "[green]on[/green] "
                console.print(f"    {status} {plugin.name}")

            console.print()
            console.print("  [bold]Skills[/bold]")
            for skill in skills:
                status = "[red]hidden[/red]" if skill.name in hidden_skills else "[green]visible[/green]"
                console.print(f"    {status} {skill.name}")

            console.print()
            console.print("[dim]Groups:[/dim] " + ", ".join(
                f"[bold]{k}[/bold] ({v['label']})" for k, v in TOOL_GROUPS.items()
            ))
            console.print()
            return CommandResult(status="handled")

        # Parse: enable/disable <name> or enable-group/disable-group <key>
        parts = args_clean.split(maxsplit=1)
        if len(parts) == 2:
            action = parts[0].lower()
            target = parts[1].strip()

            # Group operations
            if action in ("enable-group", "disable-group"):
                group_key = target.lower()
                if group_key not in TOOL_GROUPS:
                    console.print(f"[red]Unknown group: {group_key}[/red]")
                    console.print("[dim]Groups: " + ", ".join(TOOL_GROUPS.keys()) + "[/dim]")
                    console.print()
                    return CommandResult(status="handled")

                group_label = TOOL_GROUPS[group_key]["label"]
                if action == "disable-group":
                    changed = ToolRegistry.disable_group(group_key)
                    if changed:
                        console.print(f"[yellow]Disabled {group_label}:[/yellow] {', '.join(changed)}")
                    else:
                        console.print(f"[dim]All {group_label} tools already disabled.[/dim]")
                else:
                    changed = ToolRegistry.enable_group(group_key)
                    if changed:
                        console.print(f"[green]Enabled {group_label}:[/green] {', '.join(changed)}")
                    else:
                        console.print(f"[dim]All {group_label} tools already enabled.[/dim]")

                # Sync and persist
                tool_settings.disabled_tools = sorted(ToolRegistry.get_disabled())
                _persist_tool_visibility(console)
                console.print()
                return CommandResult(status="handled")

            # Single tool/plugin operations
            if action in ("enable", "disable"):
                all_registered_lower = {t.name.lower(): t.name for t in ToolRegistry._tools.values()}
                all_registered_lower.update({t.name.lower(): t.name for t in plugin_manifest.get_all()})
                matched = all_registered_lower.get(target.lower())
                if not matched:
                    console.print(f"[red]Unknown tool or plugin: {target}[/red]")
                    console.print(f"[dim]Run [bold #5F9EA0]/tools list[/bold #5F9EA0] to see all tools.[/dim]")
                    return CommandResult(status="handled")

                if action == "enable":
                    ToolRegistry.enable(matched)
                    tool_settings.disabled_tools = [n for n in tool_settings.disabled_tools if n != matched]
                    console.print(f"[green]Enabled: {matched}[/green]")
                else:
                    ToolRegistry.disable(matched)
                    if matched not in tool_settings.disabled_tools:
                        tool_settings.disabled_tools.append(matched)
                    console.print(f"[yellow]Disabled: {matched}[/yellow]")

                _persist_tool_visibility(console)
                console.print()
                return CommandResult(status="handled")

            if action in ("show-skill", "hide-skill"):
                try:
                    skill_name = validate_skill_name(target)
                except Exception as e:
                    console.print(f"[red]{e}[/red]")
                    return CommandResult(status="handled")

                known_skills = {skill.name for skill in iter_skill_summaries()}
                if skill_name not in known_skills:
                    console.print(f"[red]Unknown skill: {skill_name}[/red]")
                    return CommandResult(status="handled")

                if action == "show-skill":
                    tool_settings.hidden_skills = [n for n in tool_settings.hidden_skills if n != skill_name]
                    console.print(f"[green]Skill visible in discovery: {skill_name}[/green]")
                else:
                    if skill_name not in tool_settings.hidden_skills:
                        tool_settings.hidden_skills.append(skill_name)
                    console.print(f"[yellow]Skill hidden from discovery: {skill_name}[/yellow]")

                tool_settings.hidden_skills = sorted(set(tool_settings.hidden_skills))
                _persist_tool_visibility(console)
                console.print()
                return CommandResult(status="handled")

        console.print(f"[red]Unknown subcommand: {args}[/red]")
        console.print("Usage: [bold #5F9EA0]/tools[/bold #5F9EA0] [list | enable <name> | disable <name> | show-skill <name> | hide-skill <name> | enable-group <key> | disable-group <key>]")
        return CommandResult(status="handled")

    # No args — interactive toggle UI, organized by groups
    all_tools_map = {t.name: t for t in ToolRegistry._tools.values()}
    plugin_tools = {t.name: t for t in plugin_manifest.get_all()}
    disabled = ToolRegistry.get_disabled()
    hidden_skills = set(tool_settings.hidden_skills)

    categories = []
    for gkey, gdef in TOOL_GROUPS.items():
        group_options = []
        for tname in gdef["tools"]:
            t = all_tools_map.get(tname)
            if not t:
                continue
            is_off = tname in disabled
            group_options.append(SettingOption(
                key=tname,
                text=tname,
                value=not is_off,
                input_type="boolean",
                on_text="ON",
                off_text="OFF",
            ))
        if group_options:
            categories.append(SettingCategory(title=gdef["label"], settings=group_options))

    # Catch any tools not in a group
    grouped_names = set()
    for gdef in TOOL_GROUPS.values():
        grouped_names.update(gdef["tools"])
    ungrouped = [
        t for t in sorted(all_tools_map.values(), key=lambda x: x.name)
        if t.name not in grouped_names
    ]
    if ungrouped:
        other_options = []
        for t in ungrouped:
            is_off = t.name in disabled
            other_options.append(SettingOption(
                key=t.name,
                text=t.name,
                value=not is_off,
                input_type="boolean",
                on_text="ON",
                off_text="OFF",
            ))
        categories.append(SettingCategory(title="Other", settings=other_options))

    plugin_options = []
    for name in sorted(plugin_tools):
        is_off = name in disabled
        plugin_options.append(SettingOption(
            key=name,
            text=name,
            value=not is_off,
            input_type="boolean",
            on_text="ON",
            off_text="OFF",
        ))
    if plugin_options:
        categories.append(SettingCategory(title="User plugins", settings=plugin_options))

    skill_options = []
    for skill in sorted(iter_skill_summaries(), key=lambda s: s.name):
        skill_options.append(SettingOption(
            key=f"skill:{skill.name}",
            text=skill.name,
            value=skill.name not in hidden_skills,
            input_type="boolean",
            on_text="VISIBLE",
            off_text="HIDDEN",
        ))
    if skill_options:
        categories.append(SettingCategory(title="Skills", settings=skill_options))

    selector = SettingSelector(
        categories=categories,
        title="Tool Settings",
    )

    changes = selector.run()

    if changes is None:
        console.print("[dim]Cancelled.[/dim]")
        return CommandResult(status="handled")

    if not changes:
        console.print("[dim]No changes made.[/dim]")
        return CommandResult(status="handled")

    # Apply changes
    newly_disabled = []
    newly_enabled = []
    newly_hidden_skills = []
    newly_visible_skills = []
    for name, enabled in changes.items():
        if name.startswith("skill:"):
            skill_name = name.split(":", 1)[1]
            if enabled and skill_name in hidden_skills:
                newly_visible_skills.append(skill_name)
            elif not enabled and skill_name not in hidden_skills:
                newly_hidden_skills.append(skill_name)
            continue

        if not enabled and name not in disabled:
            ToolRegistry.disable(name)
            newly_disabled.append(name)
        elif enabled and name in disabled:
            ToolRegistry.enable(name)
            newly_enabled.append(name)

    tool_settings.disabled_tools = sorted(ToolRegistry.get_disabled())
    next_hidden_skills = set(hidden_skills)
    next_hidden_skills.update(newly_hidden_skills)
    next_hidden_skills.difference_update(newly_visible_skills)
    tool_settings.hidden_skills = sorted(next_hidden_skills)

    _persist_tool_visibility(console)

    # Summary
    change_lines = []
    for name in newly_disabled:
        change_lines.append(f"  [yellow]Disabled:[/yellow] {name}")
    for name in newly_enabled:
        change_lines.append(f"  [green]Enabled:[/green] {name}")
    for name in newly_hidden_skills:
        change_lines.append(f"  [yellow]Hidden skill:[/yellow] {name}")
    for name in newly_visible_skills:
        change_lines.append(f"  [green]Visible skill:[/green] {name}")

    if change_lines:
        total_enabled = len(ToolRegistry.get_all())
        total_disabled = len(ToolRegistry.get_disabled())
        console.print(f"[green]Tools updated:[/green] ({total_enabled} enabled, {total_disabled} disabled)")
        for line in change_lines:
            console.print(line)
    else:
        console.print("[dim]No changes applied.[/dim]")

    return CommandResult(status="handled")


def _handle_cd(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle /cd command — change working directory.

    Usage: /cd <path>
    Examples:
        /cd /home/user/projects
        /cd ..
        /cd ~/Documents
    """
    import os

    if not args or not args.strip():
        # Show current working directory
        cwd = os.getcwd()
        console.print(f"[bold #5F9EA0]Current directory:[/bold #5F9EA0] {cwd}")
        return CommandResult(status="handled")

    path = args.strip()

    # Expand ~ to home directory
    path = os.path.expanduser(path)

    # Resolve to absolute path
    try:
        target_path = Path(path).resolve()
    except Exception as e:
        console.print(f"[red]Invalid path: {e}[/red]")
        return CommandResult(status="handled")

    # Check if path exists and is a directory
    if not target_path.exists():
        console.print(f"[red]Directory not found: {target_path}[/red]")
        return CommandResult(status="handled")

    if not target_path.is_dir():
        console.print(f"[red]Not a directory: {target_path}[/red]")
        return CommandResult(status="handled")

    # Change directory
    try:
        os.chdir(target_path)
        console.print(f"[green]Changed directory to: {target_path}[/green]")
    except Exception as e:
        console.print(f"[red]Failed to change directory: {e}[/red]")

    # Reset memory system singleton (project memory is per-repo)
    try:
        from core.memory import MemoryManager
        MemoryManager.reset()
    except Exception:
        pass

    # Rebuild system prompt so project root stays current
    try:
        chat_manager.update_system_prompt()
    except Exception:
        pass

    return CommandResult(status="handled")


def _handle_prompt(chat_manager, console, debug_mode_container, args, cron_scheduler=None):
    """Handle /prompt command — show/swap prompt variants."""
    from utils.settings import prompt_settings
    from llm.prompts import _variant_available, _list_variants

    cfg_manager = config_manager

    if not args or args.strip() == "list":
        variants = _list_variants()
        current = prompt_settings.variant
        console.print()
        console.print(f"[bold #5F9EA0]Prompt Variants[/bold #5F9EA0]  (current: [bold]{current}[/bold])")
        console.print()
        for v in variants:
            marker = "[bold green]active[/bold green]" if v == current else ""
            console.print(f"  [bold]{v}[/bold]  {marker}")
        console.print()
        console.print("[dim]Switch with: [bold #5F9EA0]/prompt main[/bold #5F9EA0] or [bold #5F9EA0]/prompt micro[/bold #5F9EA0][/dim]")
        return CommandResult(status="handled")

    # Single arg: variant name to switch to
    target = args.strip().lower()

    if not _variant_available(target):
        variants = _list_variants()
        console.print(f"[red]Unknown variant: '{target}'[/red]")
        console.print(f"[dim]Available: {', '.join(variants)}[/dim]")
        return CommandResult(status="handled")

    # Update settings
    prompt_settings.variant = target

    # Persist to config
    try:
        cfg_data = cfg_manager.load(force_reload=True)
        if "PROMPT_SETTINGS" not in cfg_data:
            cfg_data["PROMPT_SETTINGS"] = {}
        cfg_data["PROMPT_SETTINGS"]["variant"] = target
        cfg_manager.save(cfg_data)
    except Exception as e:
        console.print(f"[red]Failed to save variant to config: {e}[/red]")
        console.print("[yellow]Variant applied for this session only — it will revert on restart.[/yellow]")

    # Rebuild system prompt in-place (no restart)
    chat_manager.update_system_prompt(variant=target)
    console.print(f"[green]Switched to '{target}' variant[/green]")
    console.print("[dim]System prompt rebuilt in-place.[/dim]")

    return CommandResult(status="handled")


def _print_skills_usage(console):
    show_skills_help_table(console)


def _skills_list(console, query=None):
    from datetime import datetime
    from core.skills import get_skills_dir, list_skills

    skills = list_skills(query=query)
    if not skills:
        console.print("[dim]No skills found.[/dim]")
        console.print(f"[dim]Directory: {get_skills_dir()}[/dim]")
        console.print("[dim]Create one with: [bold #5F9EA0]/skills add frontend_design[/bold #5F9EA0][/dim]")
        return

    table = Table(show_header=True, box=box.SIMPLE_HEAD)
    table.add_column("Skill", no_wrap=True)
    table.add_column("Preview")
    table.add_column("Modified", no_wrap=True)
    for skill in skills:
        modified = datetime.fromtimestamp(skill.modified).strftime("%Y-%m-%d %H:%M")
        table.add_row(f"[bold]{skill.name}[/bold]", skill.preview, modified)
    console.print(table)
    console.print("[dim]Load with: [bold #5F9EA0]/skills load <name>[/bold #5F9EA0][/dim]")


def _open_skill_editor(console, debug_mode_container, initial_content):
    from utils.editor import open_editor_for_content

    return open_editor_for_content(
        console,
        initial_content=initial_content,
        debug_mode=debug_mode_container["debug"],
    )


def _write_skill_from_editor(console, debug_mode_container, name, initial_content, *, overwrite, verb):
    from core.skills import write_skill

    success, content = _open_skill_editor(console, debug_mode_container, initial_content)
    if not success or not content or not content.strip():
        console.print("[dim]Cancelled.[/dim]")
        return None
    path = write_skill(name, content, overwrite=overwrite)
    console.print(f"[green]{verb} skill '{name}'.[/green] [dim]{path}[/dim]")
    return path


def _handle_skills(chat_manager, console, debug_mode_container, args, cron_sched=None):
    """Handle /skills command — manage reusable prompt snippets."""
    from core.skills import (
        SkillError,
        activate_skill,
        get_skills_dir,
        read_skill,
        remove_skill,
        validate_skill_name,
        write_skill,
    )

    if not args or not args.strip():
        _skills_list(console)
        return CommandResult(status="handled")

    args_clean = args.strip()
    parts = args_clean.split(maxsplit=2)
    subcmd = parts[0].lower()

    try:
        if subcmd in ("help", "-h", "--help"):
            _print_skills_usage(console)
            return CommandResult(status="handled")

        if subcmd in ("list", "ls"):
            list_parts = args_clean.split(maxsplit=1)
            query = list_parts[1] if len(list_parts) > 1 else None
            _skills_list(console, query=query)
            return CommandResult(status="handled")

        if subcmd == "dir":
            console.print(str(get_skills_dir()))
            return CommandResult(status="handled")

        if subcmd == "show":
            if len(parts) < 2:
                console.print("[red]Usage: /skills show <name>[/red]")
                return CommandResult(status="handled")
            name = validate_skill_name(parts[1])
            content = read_skill(name, strip_heading=False)
            console.print(Markdown(left_align_headings(content), code_theme=MonokaiDarkBGStyle, justify="left"))
            return CommandResult(status="handled")

        if subcmd in ("load", "use"):
            if len(parts) < 2:
                console.print(f"[red]Usage: /skills {subcmd} <name>[/red]")
                return CommandResult(status="handled")
            name = validate_skill_name(parts[1])
            tokens = activate_skill(chat_manager, name, read_skill(name))
            console.print(f"[green]Activated skill '{name}' for this chat.[/green] [dim](~{tokens:,} tokens)[/dim]")
            return CommandResult(status="handled")

        if subcmd in ("remove", "rm", "delete"):
            if len(parts) < 2:
                console.print("[red]Usage: /skills remove <name>[/red]")
                return CommandResult(status="handled")
            name = validate_skill_name(parts[1])
            from rich.prompt import Confirm
            if not Confirm.ask(f"Remove skill '{name}'?", default=False):
                console.print("[dim]Cancelled.[/dim]")
                return CommandResult(status="handled")
            remove_skill(name)
            console.print(f"[green]Removed skill '{name}'.[/green]")
            return CommandResult(status="handled")

        if subcmd == "edit":
            if len(parts) < 2:
                console.print("[red]Usage: /skills edit <name>[/red]")
                return CommandResult(status="handled")
            name = validate_skill_name(parts[1])
            try:
                initial_content = read_skill(name, strip_heading=False)
            except SkillError:
                initial_content = f"# {name}\n\n"
            _write_skill_from_editor(
                console,
                debug_mode_container,
                name,
                initial_content,
                overwrite=True,
                verb="Saved",
            )
            return CommandResult(status="handled")

        if subcmd in ("add", "create", "new"):
            if len(parts) < 2:
                console.print(f"[red]Usage: /skills {subcmd} <name> [prompt][/red]")
                return CommandResult(status="handled")
            name = validate_skill_name(parts[1])
            if len(parts) >= 3:
                path = write_skill(name, parts[2], overwrite=False)
                console.print(f"[green]Created skill '{name}'.[/green] [dim]{path}[/dim]")
                return CommandResult(status="handled")
            _write_skill_from_editor(
                console,
                debug_mode_container,
                name,
                f"# {name}\n\n",
                overwrite=False,
                verb="Created",
            )
            return CommandResult(status="handled")

        if subcmd == "modify":
            cmd_parts = args_clean.split(maxsplit=2)
            if len(cmd_parts) < 2:
                console.print("[red]Usage: /skills modify <name> [prompt][/red]")
                return CommandResult(status="handled")
            name = validate_skill_name(cmd_parts[1])
            if len(cmd_parts) < 3:
                _write_skill_from_editor(
                    console,
                    debug_mode_container,
                    name,
                    read_skill(name, strip_heading=False),
                    overwrite=True,
                    verb="Updated",
                )
                return CommandResult(status="handled")
            read_skill(name)
            path = write_skill(name, cmd_parts[2], overwrite=True)
            console.print(f"[green]Updated skill '{name}'.[/green] [dim]{path}[/dim]")
            return CommandResult(status="handled")

        if len(parts) >= 2:
            name = validate_skill_name(parts[0])
            _, body = args_clean.split(maxsplit=1)
            path = write_skill(name, body, overwrite=False)
            console.print(f"[green]Created skill '{name}'.[/green] [dim]{path}[/dim]")
            return CommandResult(status="handled")

        console.print("[red]Usage: /skills add <name> [prompt][/red]")
        console.print("[dim]Run [bold #5F9EA0]/skills help[/bold #5F9EA0] for all commands.[/dim]")
        return CommandResult(status="handled")

    except SkillError as e:
        console.print(f"[red]{e}[/red]")
        return CommandResult(status="handled")
    except Exception as e:
        console.print(f"[red]Skills command failed: {e}[/red]")
        return CommandResult(status="handled")


def _handle_obsidian_init(console, obsidian_settings):
    """Handle /obsidian init — scaffold project folder structure in vault."""
    if not obsidian_settings.is_active():
        console.print("[yellow]Obsidian vault is not configured or inactive.[/yellow]")
        console.print("[dim]Run [bold #5F9EA0]/obsidian set <path>[/bold #5F9EA0] to configure your vault.[/dim]")
        console.print()
        return CommandResult(status="handled")

    # subcmd is always "init" — just do the init
    from tools.obsidian import get_vault_session

    session = get_vault_session()
    if not session:
        console.print("[red]Could not determine project folder.[/red]")
        return CommandResult(status="handled")

    project_folder = session.project_folder

    # Check if already exists
    if project_folder.is_dir():
        console.print(f"[yellow]Project folder already exists: {project_folder}[/yellow]")
        console.print("[dim]No changes made. Delete the folder manually if you want to re-initialize.[/dim]")
        console.print()
        return CommandResult(status="handled")

    # Define folder structure and templates
    folders = {
        "Bugs": (
            "---\n"
            "title: {title}\n"
            "type: bug\n"
            "status: reported\n"
            "priority: medium\n"
            "date_created: {date}\n"
            "date_modified: {date}\n"
            "tags: [bug]\n"
            "---\n"
            "\n"
            "# {title}\n"
            "\n"
            "**Description:**\n"
            "\n"
            "**Steps to reproduce:**\n"
            "\n"
            "**Expected behavior:**\n"
            "\n"
            "**Actual behavior:**\n"
            "\n"
            "Statuses: `reported` → `in-progress` → `fixed` → `verified`\n"
        ),
        "Tasks": (
            "---\n"
            "title: {title}\n"
            "type: task\n"
            "status: todo\n"
            "priority: medium\n"
            "date_created: {date}\n"
            "date_modified: {date}\n"
            "tags: [task]\n"
            "---\n"
            "\n"
            "# {title}\n"
            "\n"
            "**Description:**\n"
            "\n"
            "**Acceptance criteria:**\n"
            "\n"
            "Statuses: `todo` → `in-progress` → `done`\n"
        ),
        "Docs": (
            "---\n"
            "title: {title}\n"
            "type: doc\n"
            "date_created: {date}\n"
            "date_modified: {date}\n"
            "tags: [docs]\n"
            "---\n"
            "\n"
            "# {title}\n"
            "\n"
        ),
    }

    from datetime import date

    today = date.today().isoformat()
    created_folders = []

    for folder_rel, template in folders.items():
        folder_path = project_folder / folder_rel
        folder_path.mkdir(parents=True, exist_ok=True)
        created_folders.append(folder_rel)

        # Write template
        template_path = folder_path / "_Template.md"
        if not template_path.exists():
            title = folder_rel.split("/")[-1].rstrip("s")
            content = template.format(date=today, title=title)
            template_path.write_text(content, encoding="utf-8")

    # Create Done/ subfolders for archiving completed notes
    for folder_rel in ("Bugs", "Tasks"):
        done_path = project_folder / folder_rel / "Done"
        done_path.mkdir(parents=True, exist_ok=True)
        created_folders.append(f"{folder_rel}/Done")

    # Create Dashboard
    dashboard_path = project_folder / "Dashboard.md"
    repo_name = project_folder.name
    dv_tasks = (
        f'```dataview\n'
        f"TABLE status, priority, date_created\n"
        f'FROM "{session.project_folder_relative}/Tasks"\n'
        f'WHERE type = "task" AND status != "done"\n'
        f"SORT date_created DESC\n"
        f"```\n"
    )
    dv_bugs = (
        f'```dataview\n'
        f"TABLE status, priority, date_created\n"
        f'FROM "{session.project_folder_relative}/Bugs"\n'
        f'WHERE type = "bug" AND status != "fixed" AND status != "verified"\n'
        f"SORT date_created DESC\n"
        f"```\n"
    )
    dv_completed = (
        f'```dataview\n'
        f"TABLE type, status, date_modified\n"
        f'FROM "{session.project_folder_relative}"\n'
        f'WHERE (type = "task" AND status = "done")\n'
        f'   OR (type = "bug" AND (status = "fixed" OR status = "verified"))\n'
        f"SORT date_modified DESC\n"
        f"```\n"
    )
    dashboard_content = (
        "---\n"
        "type: dashboard\n"
        "date_created: {date}\n"
        "date_modified: {date}\n"
        "tags: [dashboard]\n"
        "---\n"
        "\n"
        "# {title} Dashboard\n"
        "\n"
        "> [!summary] Project Overview\n"
        "> Check the Bugs/ and Tasks/ folders for issue tracking.\n"
        "\n"
        "## Open Tasks\n"
        "\n"
        f"{dv_tasks}\n"
        "## Open Bugs\n"
        "\n"
        f"{dv_bugs}\n"
        "## Recently Completed\n"
        "\n"
        f"{dv_completed}\n"
    )
    dashboard_content = dashboard_content.format(date=today, title=repo_name)
    dashboard_path.write_text(dashboard_content, encoding="utf-8")
    created_folders.append("Dashboard.md")

    console.print(f"[green]Project initialized: {project_folder.name}[/green]")
    for folder in created_folders:
        console.print(f"  [dim]Created: {folder}/ (_Template.md)[/dim]")
    console.print()

    # Check if Dataview plugin is installed and enabled
    vault_root = session.vault_root
    community_plugins = vault_root / ".obsidian" / "community-plugins.json"
    dataview_dir = vault_root / ".obsidian" / "plugins" / "dataview"
    has_plugin_entry = (
        community_plugins.is_file()
        and "dataview" in community_plugins.read_text(encoding="utf-8")
    )
    has_plugin_files = (
        dataview_dir.is_dir()
        and (dataview_dir / "main.js").is_file()
        and (dataview_dir / "manifest.json").is_file()
    )
    if not has_plugin_entry or not has_plugin_files:
        console.print("[yellow]Dataview plugin not detected — dashboard tables won't render.[/yellow]")
        console.print("[dim]Install the Dataview community plugin in Obsidian:[/dim]")
        console.print("[dim]  Settings → Community plugins → Browse → search 'Dataview' → Install & Enable[/dim]")
        console.print("[dim]Or download from: https://github.com/blacksmithgu/obsidian-dataview[/dim]")
        console.print()

    console.print("[dim]Create issues with [bold #5F9EA0]/obsidian init[/bold #5F9EA0] to set up the project folder.[/dim]")
    console.print()
    return CommandResult(status="handled")


# Command registry - maps command names to their handlers
_COMMAND_HANDLERS = {
    "/exit": _handle_exit,
    "/quit": _handle_exit,
    "/help": _handle_help,
    "/h": _handle_help,
    "/compact": _handle_compact,
    "/clear": _handle_clear,
    "/new": _handle_clear,
    "/reset": _handle_clear,
    "/provider": _handle_provider,
    "/config": _handle_config,

    "/edit": _handle_edit,
    "/e": _handle_edit,
    "/usage": _handle_usage,
    "/model": _handle_model,
    "/key": _handle_key,
    "/review": _handle_review,
    "/r": _handle_review,
    "/signup": _handle_signup,
    "/login": _handle_login,
    "/resend": _handle_resend,
    "/reset-key": _handle_reset_key,
    "/account": _handle_account,
    "/plan": _handle_plan,
    "/manage": _handle_manage,
    "/upgrade": _handle_upgrade,
    "/rotate-key": _handle_rotate_key,
    "/obsidian": _handle_obsidian,
    "/tools": _handle_tools,
    "/cd": _handle_cd,
    "/setup": _handle_setup,
    "/cron": _handle_cron,
    "/prompt": _handle_prompt,
    "/skills": _handle_skills,
    "/skill": _handle_skills,
}


def _handle_shell_command(console, command):
    """Execute a shell command prefixed with : and display output."""
    from utils.settings import tool_settings
    try:
        result = subprocess.run(
            ["/bin/sh", "-c", command], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=tool_settings.command_timeout_sec,
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip() or "(no output)"
        lines = output.splitlines()
        if len(lines) > 200:
            output = "\n".join(lines[:100]) + f"\n\n... ({len(lines) - 200} lines omitted) ...\n\n" + "\n".join(lines[-100:])
        console.print()
        if result.returncode != 0:
            console.print(f"[red]exit code: {result.returncode}[/red]")
        console.print(output)
        console.print()
    except subprocess.TimeoutExpired:
        console.print(f"[red]Command timed out after {tool_settings.command_timeout_sec}s[/red]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
    return CommandResult(status="handled")


def process_command(chat_manager, user_input, console, debug_mode_container, cron_scheduler=None):
    """Process command and optionally return replacement content.

    Args:
        chat_manager: ChatManager instance
        user_input: User's input string
        console: Rich console for output
        debug_mode_container: Dict with 'debug' key for debug mode state
        cron_scheduler: Optional CronScheduler instance for immediate reload

    Returns:
        tuple: (status, replacement_content)
            status: "exit" | "handled" | None
            replacement_content: str to replace user_input, or None
    """
    # Parse command and arguments
    parts = user_input.split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else None

    # Shell command prefix (:command)
    if user_input.startswith(":"):
        shell_cmd = user_input[1:].strip()
        if shell_cmd:
            result = _handle_shell_command(console, shell_cmd)
            return (result.status, result.replacement_input)
        return ("handled", None)

    # Look up handler in registry
    handler = _COMMAND_HANDLERS.get(cmd)
    if handler:
        result = handler(chat_manager, console, debug_mode_container, args, cron_scheduler)
        return (result.status, result.replacement_input)
    elif cmd.startswith('/'):
        console.print(f"[red]Unknown command: {user_input}[/red]")
        console.print("[dim]Type [bold #5F9EA0]/help[/bold #5F9EA0] for available commands[/dim]")
        return ("handled", None)

    return (None, None)
