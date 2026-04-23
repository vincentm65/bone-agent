"""UI display functions for command outputs.""" 

from rich.table import Table
from rich.panel import Panel
from rich import box
from llm import config


def show_provider_table(current_provider: str, console):
    """Display provider status table.

    Args:
        current_provider: Name of the currently active provider.
        console: Rich Console instance for output.
    """
    table = Table("Provider", "Status", "Details", title="Providers", box=box.SIMPLE_HEAD)
    for provider in config.get_providers():
        cfg = config.get_provider_config(provider)
        model = cfg.get("model", "N/A")
        if provider == "local":
            status = "✅" if cfg.get("model") else "❌ (set model path)"
        else:
            status = "✅" if cfg.get("api_key") else "❌ (set API key)"
        active = " [green](active)[/green]" if provider == current_provider else ""
        table.add_row(provider.capitalize(), status, f"{model[:40]}{active}")

    console.print(table)

    help_text = """Usage: /provider <name>

Opens an editor to configure model, API key, and costs.

Examples:
  /provider openrouter
  /provider glm
  /provider local
  /provider gemini
  /provider minimax
  /provider anthropic
  /provider kimi"""
    console.print(Panel(help_text, title="[bold #5F9EA0]Provider Settings[/bold #5F9EA0]", border_style="grey23", padding=(0, 2)))
    console.print("")


def show_help_table(console):
    """Display command help table.

    Args:
        console: Rich Console instance for output.
    """
    console.print("")
    table = Table(show_header=True, box=box.SIMPLE_HEAD)
    table.add_column("Command", no_wrap=True)
    table.add_column("Description")

    table.add_row("[bold #5F9EA0]/help[/bold #5F9EA0]", "Show help")
    table.add_row("[bold #5F9EA0]/exit[/bold #5F9EA0]", "Exit chat")
    table.add_row("[bold #5F9EA0]/config[/bold #5F9EA0]", "Show all configuration settings")
    table.add_row("[bold #5F9EA0]/provider[/bold #5F9EA0] [name]", "Configure provider settings (model, key, costs)")
    table.add_row("[bold #5F9EA0]/key[/bold #5F9EA0] <key>", "Set API key for current provider")
    table.add_row("[bold #5F9EA0]/model[/bold #5F9EA0] <name>", "Set model for current provider")
    table.add_row("[bold #5F9EA0]/usage[/bold #5F9EA0] [provider] [in|out] <cost>", "Set/view provider-specific token cost")
    table.add_row("[bold #5F9EA0]/compact[/bold #5F9EA0]", "Compact context with an AI summary")


    table.add_row("[bold #5F9EA0]/cd[/bold #5F9EA0] [path]", "Change working directory (no args to show current)")
    table.add_row("[bold #5F9EA0]/edit[/bold #5F9EA0], [bold #5F9EA0]/e[/bold #5F9EA0]", "Open editor for multi-line input")
    table.add_row("[bold #5F9EA0]/review[/bold #5F9EA0] [args], [bold #5F9EA0]/r[/bold #5F9EA0]", "Code review git changes (e.g. /review --staged, /review main..HEAD)")
    table.add_row("[bold #5F9EA0]/obsidian[/bold #5F9EA0] [set|enable|disable|status|init]", "Manage vault integration, scaffold project folders")
    table.add_row("[bold #5F9EA0]/tools[/bold #5F9EA0] [list|enable|disable|enable-group|disable-group]", "Toggle tools or groups (e.g. file_ops, task_mgmt)")
    table.add_row("[bold #5F9EA0]/setup[/bold #5F9EA0]", "Re-run the first-run setup wizard")
    table.add_row("[bold #5F9EA0]/cron[/bold #5F9EA0] [list|add|remove|enable|disable|run]", "Manage scheduled cron jobs")
    table.add_row("[bold #5F9EA0]:[/bold #5F9EA0]<command>", "Run a shell command (e.g. :git status)")


    console.print(Panel(table, title="[bold #5F9EA0]Commands[/bold #5F9EA0]", border_style="grey23", padding=(0, 2)))

    # Account management section
    console.print()
    acct_table = Table(show_header=True, box=box.SIMPLE_HEAD)
    acct_table.add_column("Command", no_wrap=True)
    acct_table.add_column("Description")

    acct_table.add_row("[bold #5F9EA0]/signup[/bold #5F9EA0] <email>", "Create bone-agent account and get API key")
    acct_table.add_row("[bold #5F9EA0]/login[/bold #5F9EA0]", "Log in to an existing bone-agent account")
    acct_table.add_row("[bold #5F9EA0]/account[/bold #5F9EA0]", "View account info and subscription status")
    acct_table.add_row("[bold #5F9EA0]/plan[/bold #5F9EA0]", "View available plans and pricing")
    acct_table.add_row("[bold #5F9EA0]/upgrade[/bold #5F9EA0]", "Upgrade or change your plan")
    acct_table.add_row("[bold #5F9EA0]/manage[/bold #5F9EA0]", "Cancel subscription or update payment (Stripe portal)")
    acct_table.add_row("[bold #5F9EA0]/rotate-key[/bold #5F9EA0]", "Invalidate current API key and generate a new one")
    acct_table.add_row("[bold #5F9EA0]/reset-key[/bold #5F9EA0]", "Get a new API key emailed to you (lost key recovery)")

    console.print(Panel(acct_table, title="[bold #5F9EA0]Account[/bold #5F9EA0]", border_style="grey23", padding=(0, 2)))

    # Keybinds section
    console.print()
    keybinds = Table(show_header=True, box=box.SIMPLE_HEAD)
    keybinds.add_column("Keybind", no_wrap=True)
    keybinds.add_column("Action")

    keybinds.add_row("Shift+Tab", "Cycle approval mode")
    keybinds.add_row("Ctrl+C", "Interrupt response")
    keybinds.add_row("Ctrl+C (2x)", "Exit program")

    console.print(Panel(keybinds, title="[bold #5F9EA0]Keybinds[/bold #5F9EA0]", border_style="grey23", padding=(0, 2)))
    console.print("")


def show_cron_help_table(console):
    """Display cron command help table.

    Args:
        console: Rich Console instance for output.
    """
    console.print("")
    table = Table(show_header=True, box=box.SIMPLE_HEAD)
    table.add_column("Command", no_wrap=True)
    table.add_column("Description")

    table.add_row("[bold #5F9EA0]/cron list[/bold #5F9EA0]", "Show all cron jobs (default)")
    table.add_row("[bold #5F9EA0]/cron add[/bold #5F9EA0] <id> <schedule> <cmd>", "Add a new cron job")
    table.add_row("[bold #5F9EA0]/cron remove[/bold #5F9EA0] <id>", "Remove a cron job")
    table.add_row("[bold #5F9EA0]/cron enable[/bold #5F9EA0] <id>", "Enable a cron job")
    table.add_row("[bold #5F9EA0]/cron disable[/bold #5F9EA0] <id>", "Disable a cron job")
    table.add_row("[bold #5F9EA0]/cron run[/bold #5F9EA0] <id>", "Run a job immediately (interactive)")
    table.add_row("[bold #5F9EA0]/cron allowlist[/bold #5F9EA0] [list|add|remove|clear]", "Manage allowed commands for a job")

    console.print(Panel(table, title="[bold #5F9EA0]Commands[/bold #5F9EA0]", border_style="grey23", padding=(0, 2)))

    # Schedule formats section
    console.print()
    sched_table = Table(show_header=True, box=box.SIMPLE_HEAD)
    sched_table.add_column("Format")
    sched_table.add_column("Example")

    sched_table.add_row("every <n> <unit>", "every 5 minutes, every 1 hour, every 3 days")
    sched_table.add_row("daily at <time>", "daily at 8am, daily at 17:30")
    sched_table.add_row("<day>s at <time>", "weekdays at 9am, mondays at 10:30pm")
    sched_table.add_row("<time>", "08:00, 17:30")

    console.print(Panel(sched_table, title="[bold #5F9EA0]Schedule Formats[/bold #5F9EA0]", border_style="grey23", padding=(0, 2)))
    console.print("")


def show_config_overview(chat_manager, console, debug_mode_container, current_provider):
    """Display comprehensive configuration overview.

    Args:
        chat_manager: ChatManager instance for runtime state
        console: Rich Console instance for output
        debug_mode_container: Dict with debug key for debug mode state
        current_provider: Name of the currently active provider
    """
    from core.config_manager import ConfigManager
    config_manager = ConfigManager()
    config_data = config_manager.load()

    console.print()

    # ===== Runtime Settings =====
    runtime_table = Table("Setting", "Status", title="Runtime Settings", box=box.SIMPLE_HEAD)
    debug_status = "[green]ON[/green]" if debug_mode_container.get("debug") else "[dim]OFF[/dim]"
    runtime_table.add_row("Debug Mode", debug_status)
    logging_status = "[green]ON[/green]" if chat_manager.markdown_logger else "[dim]OFF[/dim]"
    runtime_table.add_row("Conversation Logging", logging_status)
    approve_labels = {"safe": "SAFE", "accept_edits": "ACCEPT EDITS", "danger": "DANGER"}
    approve_colors = {"safe": "green", "accept_edits": "yellow", "danger": "red"}
    approve_mode = chat_manager.approve_mode
    approve_color = approve_colors.get(approve_mode, "white")
    runtime_table.add_row("Approval Mode", f"[{approve_color}]{approve_labels.get(approve_mode, approve_mode.upper())}[/{approve_color}]")
    console.print(runtime_table)

    # ===== Provider Settings =====
    console.print()
    provider_table = Table("Provider", "Model", "$ in/out", "API Key", title="Providers", box=box.SIMPLE_HEAD)

    active_provider = config_data.get("LAST_PROVIDER", "Not set").upper()
    provider_table.add_row("[green]Active[/green]", f"[green]{active_provider}[/green]", "", "")

    def fmt(v, max_len=35):
        return v[:max_len-3] + "..." if len(v) > max_len else v

    # Local provider
    local_model = config_data.get("LOCAL_MODEL_PATH", "Not set")
    provider_table.add_row("Local", fmt(local_model), "N/A", "N/A")

    # API providers
    for provider in ["OpenRouter", "GLM", "OpenAI", "Gemini", "MiniMax", "Anthropic", "Kimi"]:
        model = config_data.get(f"{provider.upper()}_MODEL", "Not set")
        key = config_data.get(f"{provider.upper()}_API_KEY", "")
        key_status = "[green]✓[/green]" if key else "[red]✗[/red]"

        # Check for model-specific pricing
        model_prices = config_data.get("MODEL_PRICES", {})
        if model and model in model_prices:
            cost_in = model_prices[model].get("cost_in", 0)
            cost_out = model_prices[model].get("cost_out", 0)
            if cost_in > 0 or cost_out > 0:
                cost_str = f"${cost_in:.2f}/${cost_out:.2f}"
            else:
                cost_str = "Not set"
        else:
            cost_str = "Not set"

        provider_table.add_row(provider, fmt(model), cost_str, key_status)

    console.print(provider_table)

    # ===== Quick Commands Reference =====
    console.print()
    help_text = """[bold #5F9EA0]Commands:[/bold #5F9EA0] [bold #5F9EA0]/provider[/bold #5F9EA0] <name>  [bold #5F9EA0]/model[/bold #5F9EA0] <path>  [bold #5F9EA0]/key[/bold #5F9EA0] <key>
[#5F9EA0]         :[/#5F9EA0] [bold #5F9EA0]/usage[/bold #5F9EA0] [provider] [in|out] <$>  [bold #5F9EA0]/config[/bold #5F9EA0]"""
    console.print(Panel(help_text, title="[#5F9EA0]Quick Reference[/#5F9EA0]"))
    console.print()
