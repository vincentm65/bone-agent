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
    console.print(Panel(help_text, title="[bold cyan]Provider Settings[/bold cyan]", border_style="grey23", padding=(0, 2)))
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

    table.add_row("[bold cyan]/help[/bold cyan]", "Show help")
    table.add_row("[bold cyan]/exit[/bold cyan]", "Exit chat")
    table.add_row("[bold cyan]/config[/bold cyan]", "Show all configuration settings")
    table.add_row("[bold cyan]/provider[/bold cyan] [name]", "Configure provider settings (model, key, costs)")
    table.add_row("[bold cyan]/key[/bold cyan] <key>", "Set API key for current provider")
    table.add_row("[bold cyan]/model[/bold cyan] <name>", "Set model for current provider")
    table.add_row("[bold cyan]/usage[/bold cyan] [provider] [in|out] <cost>", "Set/view provider-specific token cost")
    table.add_row("[bold cyan]/compact[/bold cyan] [-a]", "Compact context with an AI summary (add -a for aggressive mode)")
    table.add_row("[bold cyan]/init[/bold cyan]", "Generate agents.md")
    table.add_row("[bold cyan]/edit[/bold cyan], [bold cyan]/e[/bold cyan]", "Open editor for multi-line input")
    table.add_row("[bold cyan]/review[/bold cyan] [args], [bold cyan]/r[/bold cyan]", "Code review git changes (e.g. /review --staged, /review main..HEAD)")
    table.add_row("[bold cyan]/obsidian[/bold cyan] [set|enable|disable|status]", "Manage Obsidian vault integration")
    table.add_row("[bold cyan]/project[/bold cyan] [init|status]", "Scaffold project folders or view issue status in vault")


    console.print(Panel(table, title="[bold cyan]Commands[/bold cyan]", border_style="grey23", padding=(0, 2)))

    # Account management section
    console.print()
    acct_table = Table(show_header=True, box=box.SIMPLE_HEAD)
    acct_table.add_column("Command", no_wrap=True)
    acct_table.add_column("Description")

    acct_table.add_row("[bold cyan]/signup[/bold cyan] <email>", "Create vmcode account and get API key")
    acct_table.add_row("[bold cyan]/login[/bold cyan]", "Log in to an existing vmcode account")
    acct_table.add_row("[bold cyan]/account[/bold cyan]", "View account info and subscription status")
    acct_table.add_row("[bold cyan]/plan[/bold cyan]", "View available plans and pricing")
    acct_table.add_row("[bold cyan]/upgrade[/bold cyan]", "Upgrade or change your plan")
    acct_table.add_row("[bold cyan]/manage[/bold cyan]", "Cancel subscription or update payment (Stripe portal)")
    acct_table.add_row("[bold cyan]/rotate-key[/bold cyan]", "Invalidate current API key and generate a new one")
    acct_table.add_row("[bold cyan]/reset-key[/bold cyan]", "Get a new API key emailed to you (lost key recovery)")

    console.print(Panel(acct_table, title="[bold cyan]Account[/bold cyan]", border_style="grey23", padding=(0, 2)))

    # Keybinds section
    console.print()
    keybinds = Table(show_header=True, box=box.SIMPLE_HEAD)
    keybinds.add_column("Keybind", no_wrap=True)
    keybinds.add_column("Action")

    keybinds.add_row("Tab", "Toggle Plan/Edit mode")
    keybinds.add_row("Shift+Tab", "Cycle plan/approval mode (mode-dependent)")
    keybinds.add_row("Ctrl+C", "Interrupt response")
    keybinds.add_row("Ctrl+C (2x)", "Exit program")

    console.print(Panel(keybinds, title="[bold cyan]Keybinds[/bold cyan]", border_style="grey23", padding=(0, 2)))
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
    mode_labels = {"edit": "EDIT", "plan": "PLAN"}
    mode_colors = {"edit": "green", "plan": "cyan"}
    mode = chat_manager.interaction_mode
    mode_color = mode_colors.get(mode, "white")
    runtime_table.add_row("Interaction Mode", f"[{mode_color}]{mode_labels.get(mode, mode.upper())}[/{mode_color}]")
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
    help_text = """[bold cyan]Commands:[/bold cyan] [bold cyan]/provider[/bold cyan] <name>  [bold cyan]/model[/bold cyan] <path>  [bold cyan]/key[/bold cyan] <key>
[cyan]         :[/cyan] [bold cyan]/usage[/bold cyan] [provider] [in|out] <$>  [bold cyan]/config[/bold cyan]"""
    console.print(Panel(help_text, title="[cyan]Quick Reference[/cyan]"))
    console.print()
