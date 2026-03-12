"""Startup banner display - separated from main.py to avoid circular imports."""

import os
from rich.console import Console
from rich.theme import Theme
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from llm import config

console = Console(theme=Theme({
    "markdown.hr": "grey50",
    "markdown.heading": "default",
    "markdown.h1": "default",
    "markdown.h2": "default",
    "markdown.h3": "default",
    "markdown.h4": "default",
    "markdown.h5": "default",
    "markdown.h6": "default",
}))


def format_directory_path(path: str) -> str:
    """Format directory path to show first and last parts with ellipsis.
    
    Args:
        path: Full directory path.
        
    Returns:
        Shortened path like 'c:/.../vmCode' or full path if short enough.
    """
    parts = path.split(os.sep)
    if len(parts) > 2:
        return f"{parts[0]}.../{parts[-1]}"
    return path


def display_startup_banner(approve_mode: str, interaction_mode: str = "edit"):
    """Ultra-minimalist startup screen for vmCode.

    Args:
        approve_mode: Current approval mode setting.
        interaction_mode: Current interaction mode ('plan', 'edit', or 'learn').
    """
    console.clear()

    # Get model name based on provider
    provider_config = config.get_provider_config(config.LLM_PROVIDER)
    if config.LLM_PROVIDER == "local":
        model_path = provider_config.get("model") or ""
        model_name = os.path.basename(model_path) if model_path else "None"
    else:
        model_name = provider_config.get("model") or "None"

    # Get and format current directory
    current_dir = os.getcwd()
    formatted_dir = format_directory_path(current_dir)

    # Create grid for 2-column layout
    grid = Table.grid(expand=True)
    grid.add_column(justify="left", ratio=1)
    grid.add_column(justify="right", ratio=1)

    # Add content
    grid.add_row(
        Text("vmCode", style="bold white"),
        Text("v1.0.3", style="dim white")
    )

    model_info = Text.assemble(
        (f"{config.LLM_PROVIDER.upper()} ", "bold cyan"),
        (f"{model_name}", "grey70")
    )

    grid.add_row(
        model_info,
        Text(formatted_dir, style="dim grey50")
    )

    # Display in panel
    console.print(Panel(
        grid,
        border_style="grey23",
        padding=(0, 2)
    ))

    # Show vmcode_free notice if using free tier
    if config.LLM_PROVIDER == "vmcode_free":
        console.print(Panel(
            Text.assemble(
                ("✨ ", "bright_yellow"),
                ("Using vmCode Free Model", "bold bright_yellow"),
                ("\n\n", ""),
                ("No API key required • Conversations routed through vmCode proxy\n", "dim"),
                ("Switch providers with ", "dim"),
                ("/provider", "bold cyan"),
                (" command", "dim")
            ),
            border_style="bright_yellow",
            padding=(1, 2)
        ))
        console.print()

