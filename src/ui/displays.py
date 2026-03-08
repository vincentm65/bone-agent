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
        model = cfg.get('model', 'N/A')
        if provider == 'local':
            status = '✅' if cfg.get('model') else '❌ (set model path)'
        else:
            status = '✅' if cfg.get('api_key') else '❌ (set API key)'
        active = ' [green](active)[/green]' if provider == current_provider else ''
        table.add_row(provider.capitalize(), status, f"{model[:40]}{active}")

    console.print(table)

    help_text = """Usage: /provider <name>

Examples:
  /provider openrouter
  /provider glm
  /provider local
  /provider gemini
  /provider minimax
  /provider anthropic
  /provider kimi

After switching, use:
  /key <api_key>     - Set API key for current provider
  /model <model>     - Set model for current provider"""
    console.print(Panel(help_text, title="[cyan]Switch Provider[/cyan]"))
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

    table.add_row("/help", "Show help")
    table.add_row("/exit", "Exit chat")
    table.add_row("/debug", "Toggle debug")
    table.add_row("/mode", "Toggle Plan/Edit/Learn mode")
    table.add_row("/logging", "Toggle conversation logging")
    table.add_row("/config", "Show all configuration settings")
    table.add_row("/provider [name]", "Switch provider or show provider table")
    table.add_row("/key <key>", "Set API key for current provider")
    table.add_row("/model <name>", "Set model for current provider")
    table.add_row("/usage [provider] [in|out] <cost>", "Set/view provider-specific token cost")
    table.add_row("/compact [-a]", "Compact context with an AI summary (add -a for aggressive mode)")
    table.add_row("/init", "Generate agents.md")
    table.add_row("/edit, /e", "Open editor for multi-line input")

    console.print(Panel(table, title="[bold cyan]Commands[/bold cyan]", border_style="grey23", padding=(0, 2)))

    # Keybinds section
    console.print()
    keybinds = Table(show_header=True, box=box.SIMPLE_HEAD)
    keybinds.add_column("Keybind", no_wrap=True)
    keybinds.add_column("Action")

    keybinds.add_row("Tab", "Toggle Plan/Edit/Learn mode")
    keybinds.add_row("Shift+Tab", "Cycle plan/approval/learning mode (mode-dependent)")
    keybinds.add_row("Ctrl+C", "Interrupt response")
    keybinds.add_row("Ctrl+C (2x)", "Exit program")

    console.print(Panel(keybinds, title="[bold cyan]Keybinds[/bold cyan]", border_style="grey23", padding=(0, 2)))
    console.print("")


def show_config_overview(chat_manager, console, debug_mode_container, current_provider):
    """Display comprehensive configuration overview.

    Args:
        chat_manager: ChatManager instance for runtime state
        console: Rich Console instance for output
        debug_mode_container: Dict with 'debug' key for debug mode state
        current_provider: Name of the currently active provider
    """
    from core.config_manager import ConfigManager
    config_manager = ConfigManager()
    config_data = config_manager.load()

    console.print()
    
    # ===== Runtime Settings =====
    runtime_table = Table("Setting", "Status", title="Runtime Settings", box=box.SIMPLE_HEAD)
    debug_status = "[green]ON[/green]" if debug_mode_container.get('debug') else "[dim]OFF[/dim]"
    runtime_table.add_row("Debug Mode", debug_status)
    logging_status = "[green]ON[/green]" if chat_manager.markdown_logger else "[dim]OFF[/dim]"
    runtime_table.add_row("Conversation Logging", logging_status)
    mode_labels = {"edit": "EDIT", "plan": "PLAN", "learn": "LEARN"}
    mode_colors = {"edit": "green", "plan": "cyan", "learn": "magenta"}
    mode = chat_manager.interaction_mode
    mode_color = mode_colors.get(mode, "white")
    runtime_table.add_row("Interaction Mode", f"[{mode_color}]{mode_labels.get(mode, mode.upper())}[/{mode_color}]")
    approve_labels = {"safe": "SAFE", "normal": "NORMAL", "danger": "DANGER"}
    approve_colors = {"safe": "green", "normal": "yellow", "danger": "red"}
    approve_mode = chat_manager.approve_mode
    approve_color = approve_colors.get(approve_mode, "white")
    runtime_table.add_row("Approval Mode", f"[{approve_color}]{approve_labels.get(approve_mode, approve_mode.upper())}[/{approve_color}]")
    console.print(runtime_table)

    # ===== Provider Settings =====
    console.print()
    provider_table = Table("Provider", "Model", "$ in/out", "API Key", title="Providers", box=box.SIMPLE_HEAD)

    active_provider = config_data.get('LAST_PROVIDER', 'Not set').upper()
    provider_table.add_row("[green]Active[/green]", f"[green]{active_provider}[/green]", "", "")

    def fmt(v, max_len=35):
        return v[:max_len-3] + "..." if len(v) > max_len else v

    # Local provider
    local_model = config_data.get('LOCAL_MODEL_PATH', 'Not set')
    provider_table.add_row("Local", fmt(local_model), "N/A", "N/A")

    # API providers
    for provider in ["OpenRouter", "GLM", "OpenAI", "Gemini", "MiniMax", "Anthropic", "Kimi"]:
        model = config_data.get(f'{provider.upper()}_MODEL', 'Not set')
        key = config_data.get(f'{provider.upper()}_API_KEY', '')
        key_status = "[green]✓[/green]" if key else "[red]✗[/red]"

        # Check for model-specific pricing
        model_prices = config_data.get('MODEL_PRICES', {})
        if model and model in model_prices:
            cost_in = model_prices[model].get('cost_in', 0)
            cost_out = model_prices[model].get('cost_out', 0)
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
    help_text = """[bold cyan]Commands:[/bold cyan] /provider <name>  /model <path>  /key <key>
[cyan]         :[/cyan] /usage [provider] [in|out] <$>  /debug  /mode  /logging """
    console.print(Panel(help_text, title="[cyan]Quick Reference[/cyan]"))
    console.print()

