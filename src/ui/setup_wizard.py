"""Interactive first-run setup wizard.

Guides new users through provider selection, API key entry,
Obsidian vault configuration, and optional settings using
prompt_toolkit prompts and rich console output.
"""

from pathlib import Path
from typing import Optional

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.formatted_text import FormattedText
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from llm import config as llm_config

# Provider metadata for wizard display
WIZARD_PROVIDERS = [
    ("bone", "bone-agent (free tier — no key needed)", "api"),
    ("openai", "OpenAI", "api"),
    ("anthropic", "Anthropic (Claude)", "api"),
    ("gemini", "Google Gemini", "api"),
    ("openrouter", "OpenRouter (multi-provider)", "api"),
    ("glm", "GLM (Zhipu AI)", "api"),
    ("kimi", "Kimi (Moonshot AI)", "api"),
    ("minimax", "MiniMax", "api"),
    ("local", "Local model (llama.cpp)", "local"),
]

# API providers that need a key (skip bone free tier and local)


def _resolve_config_path() -> Path:
    """Resolve config.yaml path (delegates to llm_config)."""
    return llm_config.resolve_config_path()


def is_first_run() -> bool:
    """Return True if config.yaml does not exist."""
    return not _resolve_config_path().exists()


def _prompt(console: Console, message: str, default: str = "", password: bool = False) -> str:
    """Prompt the user using prompt_toolkit with rich-styled message.

    Falls back to simple input() if prompt_toolkit fails.
    """
    try:
        suffix = f" [{default}]" if default else ""
        if password:
            return pt_prompt(
                FormattedText([("class:prompt", f"{message}{suffix}: ")]),
                is_password=True,
            ).strip() or default
        return pt_prompt(
            FormattedText([("class:prompt", f"{message}{suffix}: ")]),
        ).strip() or default
    except (EOFError, KeyboardInterrupt):
        raise
    except Exception:
        # Fallback for environments where prompt_toolkit misbehaves
        suffix = f" [{default}]" if default else ""
        if password:
            import getpass
            return getpass.getpass(f"{message}{suffix}: ").strip() or default
        return input(f"{message}{suffix}: ").strip() or default


def _select_provider(console: Console) -> str:
    """Interactive provider selection. Returns provider ID."""
    console.print()
    console.print(Panel(
        Text.from_markup("[bold #5F9EA0]Select a provider[/bold #5F9EA0]\n\n"
                         "Enter the number or provider name."),
        title="Step 1 of 3",
        border_style="grey23",
        padding=(0, 2),
        title_align="left",
    ))

    for i, (pid, label, ptype) in enumerate(WIZARD_PROVIDERS, 1):
        marker = "[dim]free[/dim]" if pid == "bone" else ""
        console.print(f"  [bold]{i:>2}[/bold]. {label} {marker}")

    console.print()

    while True:
        choice = _prompt(console, "Provider", default="1")
        # Try numeric lookup
        try:
            idx = int(choice)
            if 1 <= idx <= len(WIZARD_PROVIDERS):
                selected = WIZARD_PROVIDERS[idx - 1]
                console.print(f"  [green]Selected:[/green] {selected[1]}")
                return selected[0]
        except ValueError:
            pass
        # Try name lookup
        choice_lower = choice.lower().strip()
        for pid, label, ptype in WIZARD_PROVIDERS:
            if choice_lower in (pid, pid.split("_")[0]):
                console.print(f"  [green]Selected:[/green] {label}")
                return pid

        console.print(f"  [red]Invalid choice: '{choice}'. Enter a number or provider name.[/red]")


def _prompt_api_key(console: Console, provider_id: str) -> str:
    """Prompt for API key if the provider requires one."""
    # bone free tier and local don't need a key
    if provider_id == "bone":
        console.print("\n  [dim]bone-agent free tier — no API key required.[/dim]")
        return ""
    if provider_id == "local":
        console.print("\n  [dim]Local model — no API key required.[/dim]")
        return ""

    provider_label = next((label for pid, label, _ in WIZARD_PROVIDERS if pid == provider_id), provider_id)
    console.print()
    console.print(Panel(
        Text.from_markup(f"Enter your [bold]{provider_label}[/bold] API key."),
        title="Step 2 of 3",
        border_style="grey23",
        padding=(0, 2),
        title_align="left",
    ))

    key = _prompt(console, "API key", password=True)
    if not key:
        console.print("  [yellow]No key entered — you can set it later with [bold]/key[/bold].[/yellow]")
    return key


def _prompt_obsidian(console: Console) -> tuple[bool, str]:
    """Prompt for Obsidian vault settings. Returns (enabled, vault_path)."""
    console.print()
    console.print(Panel(
        Text.from_markup("Enable Obsidian vault integration?\n"
                         "This lets bone-agent create project notes in your vault."),
        title="Step 3 of 3",
        border_style="grey23",
        padding=(0, 2),
        title_align="left",
    ))

    enable = _prompt(console, "Enable Obsidian? (y/n)", default="n").lower().startswith("y")

    if not enable:
        console.print("  [dim]Obsidian integration disabled. Enable later with [bold]/obsidian[/bold].[/dim]")
        return (False, "")

    while True:
        vault_path = _prompt(console, "Vault path", default="~/Vault")
        expanded = Path(vault_path).expanduser().resolve()

        if expanded.is_dir():
            # Check for .obsidian folder as validation
            obsidian_marker = expanded / ".obsidian"
            if obsidian_marker.is_dir():
                console.print(f"  [green]Valid Obsidian vault detected.[/green]")
            else:
                console.print(f"  [yellow]Directory exists but no .obsidian/ folder found.[/yellow]")
                console.print(f"  [yellow]Make sure this is your Obsidian vault root.[/yellow]")
        else:
            console.print(f"  [yellow]Directory does not exist yet: {expanded}[/yellow]")
            console.print(f"  [yellow]It will be created when needed.[/yellow]")

        confirm = _prompt(console, "Confirm this path? (y/n)", default="y").lower().startswith("y")
        if confirm:
            return (True, str(expanded))

        retry = _prompt(console, "Try another path? (y/n)", default="y").lower().startswith("y")
        if not retry:
            return (False, "")


def write_config(provider_id: str, api_key: str = "",
                 obsidian_enabled: bool = False, obsidian_path: str = "") -> Path:
    """Generate and write config.yaml from wizard responses.

    Uses generate_config_template() for the base, then overlays
    user selections.

    Returns:
        Path to the written config file.
    """
    config_data = llm_config.generate_config_template()

    # Set the chosen provider
    config_data["LAST_PROVIDER"] = provider_id

    # Set API key if provided
    if api_key:
        # Map provider ID to its API key config field
        key_map = {
            "openrouter": "OPENROUTER_API_KEY",
            "glm": "GLM_API_KEY",
            "openai": "OPENAI_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "minimax": "MINIMAX_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "kimi": "KIMI_API_KEY",
            "bone": "BONE_PROXY_API_KEY",
        }
        config_key = key_map.get(provider_id)
        if config_key:
            config_data[config_key] = api_key

    # Set Obsidian settings
    config_data["OBSIDIAN_SETTINGS"] = {
        "enabled": obsidian_enabled,
        "vault_path": obsidian_path,
        "exclude_folders": ".obsidian,.trash,node_modules,.git,__pycache__",
        "project_base": "Dev",
    }

    config_path = _resolve_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    import yaml
    with open(config_path, "w", encoding="utf-8-sig") as f:
        yaml.dump(config_data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return config_path


def run_wizard(console: Optional[Console] = None) -> bool:
    """Run the interactive setup wizard.

    Args:
        console: Rich console instance. Uses a new one if None.

    Returns:
        True if wizard completed successfully, False if user aborted.
    """
    if console is None:
        console = Console()

    console.print()
    console.print(Panel(
        Text.from_markup(
            "[bold #5F9EA0]Welcome to bone-agent![/bold #5F9EA0]\n\n"
            "Let's get you set up. This will take about a minute.\n"
            "You can always change settings later with [bold]/config[/bold]."
        ),
        border_style="grey23",
        padding=(0, 2),
        title_align="left",
    ))

    try:
        # Step 1: Provider
        provider_id = _select_provider(console)

        # Step 2: API key
        api_key = _prompt_api_key(console, provider_id)

        # Step 3: Obsidian
        obsidian_enabled, obsidian_path = _prompt_obsidian(console)

        # Write config
        config_path = write_config(
            provider_id=provider_id,
            api_key=api_key,
            obsidian_enabled=obsidian_enabled,
            obsidian_path=obsidian_path,
        )

        console.print()
        console.print(Panel(
            Text.from_markup(
                f"[bold green]Setup complete![/bold green]\n\n"
                f"Config written to: [dim]{config_path}[/dim]\n"
                f"Provider: [bold]{provider_id}[/bold]\n"
                f"Obsidian: {'enabled' if obsidian_enabled else 'disabled'}\n\n"
                f"[dim]Tip: Use [bold]/provider[/bold] to switch providers,[/dim]\n"
                f"[dim]     [bold]/key[/bold] to update API keys,[/dim]\n"
                f"[dim]     [bold]/obsidian[/bold] to reconfigure vault.[/dim]"
            ),
            border_style="grey23",
            padding=(0, 2),
            title_align="left",
        ))
        console.print()

        return True

    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Setup cancelled.[/yellow]")
        return False
