"""Command routing and help display."""

from pathlib import Path
from dataclasses import dataclass
from typing import Optional
from llm import config
from core.init import run_init
from core.config_manager import ConfigManager as ConfigManagerClass
from ui.displays import show_help_table, show_provider_table, show_config_overview
from ui.banner import display_startup_banner

# Global ConfigManager instance
config_manager = ConfigManagerClass()


@dataclass
class CommandResult:
    """Standardized command return type."""
    status: str  # "exit", "handled", or "continue"
    replacement_input: Optional[str] = None  # For /edit command

# Command handler functions

def _handle_exit(chat_manager, console, debug_mode_container, args):
    """Handle exit command."""
    return CommandResult(status="exit")


def _handle_help(chat_manager, console, debug_mode_container, args):
    """Handle help command."""
    show_help_table(console)
    return CommandResult(status="handled")


def _handle_debug(chat_manager, console, debug_mode_container, args):
    """Handle debug toggle command."""
    debug_mode_container['debug'] = not debug_mode_container['debug']
    status = "enabled" if debug_mode_container['debug'] else "disabled"
    console.print(f"[yellow]Debug mode {status}[/yellow]")
    return CommandResult(status="handled")

def _handle_compact(chat_manager, console, debug_mode_container, args):
    """Handle manual context compaction."""
    # Parse args for aggressive mode
    aggressive = False
    if args:
        args_clean = args.strip().lower()
        if args_clean in ('-a', '--aggressive'):
            aggressive = True

    # Show current context summary immediately using the same format as the status bar
    num_messages = len(chat_manager.messages)
    tokens_curr = chat_manager.token_tracker.current_context_tokens
    console.print(
        "Current context summary:"
        f"\n  Messages: {num_messages}"
        f"\n  Curr: {tokens_curr:,}"
    )
    console.print()  # Spacer line

    if aggressive:
        console.print("[yellow]Aggressive mode: Compacting recent tool results too[/yellow]")
        console.print()

    result = chat_manager.compact_history(console=console, trigger="manual", aggressive=aggressive)
    if not result:
        console.print("[yellow]Nothing to compact.[/yellow]")
        return CommandResult(status="handled")

    mode_text = " (aggressive)" if aggressive else ""
    console.print(
        f"[green]Session reset{mode_text}: "
        f"{result['before_tokens']:,} -> {result['after_tokens']:,} tokens[/green]"
    )
    
    # Show the compacted summary in debug mode
    if debug_mode_container.get('debug') and 'summary' in result:
        console.print()
        console.print("[cyan]Compacted summary:[/cyan]")
        console.print(f"[dim]{result['summary']}[/dim]")
    
    return CommandResult(status="handled")


def _handle_mode(chat_manager, console, debug_mode_container, args):
    """Handle interaction mode toggle command."""
    new_mode = chat_manager.toggle_interaction_mode()

    labels = {
            "edit": "EDIT (Full Access)",
            "plan": "PLAN (Read-Only)",
            }
    colors = {
            "edit": "green",
            "plan": "cyan",
            }

    label = labels.get(new_mode, new_mode.upper())
    color = colors.get(new_mode, "white")

    console.print(f"[{color}]Interaction Mode: {label}[/{color}]")
    display_startup_banner(chat_manager.approve_mode, chat_manager.interaction_mode)
    return CommandResult(status="handled")


def _handle_logging(chat_manager, console, debug_mode_container, args):
    """Handle logging toggle command."""
    is_enabled = chat_manager.toggle_logging()

    if is_enabled:
        console.print("[green]Conversation logging enabled.[/green]")
    else:
        console.print("[dim]Conversation logging disabled.[/dim]")

    return CommandResult(status="handled")





def _handle_config(chat_manager, console, debug_mode_container, args):
    """Handle config overview command - display all settings."""
    current_provider = getattr(chat_manager.client, 'provider', 'unknown')
    show_config_overview(chat_manager, console, debug_mode_container, current_provider)
    return CommandResult(status="handled")


def _handle_clear(chat_manager, console, debug_mode_container, args):
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

    # Display cost if configured
    if costs['in'] > 0 or costs['out'] > 0:
        conv_cost = chat_manager.token_tracker.calculate_conversation_cost(costs['in'], costs['out'])
        console.print(f"  Cost: ${conv_cost['total_cost']:.4f}")

    console.print()

    chat_manager.reset_session()
    display_startup_banner(chat_manager.approve_mode, chat_manager.interaction_mode)
    return CommandResult(status="handled")


def _handle_provider(chat_manager, console, debug_mode_container, args):
    """Handle provider switching command."""
    if args:
        provider = args.strip().lower()

        # Validate provider name
        if provider not in config.get_providers():
            console.print(f"[red]Error: Unknown provider '{provider}'[/red]")
            console.print(f"[dim]Available providers: {', '.join(config.get_providers())}[/dim]")
            return CommandResult(status="handled")

        # Switch provider
        result = chat_manager.switch_provider(provider)

        # Save provider choice after successful switch
        if "Failed" not in result and "failed" not in result:
            config_manager.set_provider(provider)
            # Reload config and update client
            chat_manager.reload_config()

        # Clear screen and show banner after provider change
        display_startup_banner(chat_manager.approve_mode, chat_manager.interaction_mode)
        console.print(f"[yellow]{result}[/yellow]")

        # Show helpful next steps
        cfg = config.get_provider_config(provider)
        if provider == "local":
            if not cfg.get('model'):
                console.print("[dim]Tip: Set model path with [bold cyan]/model[/bold cyan] <path_to_gguf>[/dim]")
        else:
            if not cfg.get('api_key'):
                console.print("[dim]Tip: Set API key with [bold cyan]/key[/bold cyan] <your_api_key>[/dim]")
            if not cfg.get('model'):
                console.print("[dim]Tip: Set model with [bold cyan]/model[/bold cyan] <model_name>[/dim]")
    else:
        current = getattr(chat_manager.client, 'provider', 'unknown')
        show_provider_table(current, console)

    return CommandResult(status="handled")


def _handle_model(chat_manager, console, debug_mode_container, args):
    """Handle model setting command."""
    if not args:
        # Show current model for current provider
        current_provider = getattr(chat_manager.client, 'provider', 'unknown')
        cfg = config.get_provider_config(current_provider)
        model = cfg.get('model') or cfg.get('api_model') or 'Not set'
        console.print(f"[bold cyan]Current provider:[/bold cyan] {current_provider}")
        console.print(f"[bold cyan]Current model:[/bold cyan] {model}")
        return CommandResult(status="handled")

    model = args.strip()

    # Set model for current provider
    current_provider = getattr(chat_manager.client, 'provider', 'unknown')

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


def _handle_key(chat_manager, console, debug_mode_container, args):
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
                console.print(f"[bold cyan]Current provider:[/bold cyan] {current_provider}")
                console.print(f"[bold cyan]API key:[/bold cyan] {masked}")
            else:
                console.print(f"[bold cyan]Current provider:[/bold cyan] {current_provider}")
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


def _handle_init(chat_manager, console, debug_mode_container, args):
    """Handle init command."""
    repo_root = Path.cwd()
    run_init(repo_root, console)
    chat_manager._init_messages(reset_totals=False)  # Reload agents.md into context
    return CommandResult(status="handled")


def _handle_edit(chat_manager, console, debug_mode_container, args):
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





def _handle_usage(chat_manager, console, debug_mode_container, args):
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
    costs = config_manager.get_model_price(current_model)
    tracker = chat_manager.token_tracker

    # Display token counts
    console.print(f"[cyan]Session Token Usage ({current_model}):[/cyan]")
    console.print(f"  Input tokens:  {tracker.total_prompt_tokens:,}")
    console.print(f"  Output tokens: {tracker.total_completion_tokens:,}")
    console.print(f"  Total tokens:  {tracker.total_tokens:,}")
    console.print()
    

    # Display costs if configured
    if costs['in'] > 0 or costs['out'] > 0:
        session_cost = tracker.calculate_session_cost(costs['in'], costs['out'])
        console.print(f"[cyan]Session Cost ({current_model}):[/cyan]")

        if costs['in'] > 0:
            console.print(f"  Input:  ${session_cost['input_cost']:.6f} (${costs['in']:.6f}/1M tokens)")

        if costs['out'] > 0:
            console.print(f"  Output: ${session_cost['output_cost']:.6f} (${costs['out']:.6f}/1M tokens)")

        console.print(f"  Total:  ${session_cost['total_cost']:.6f}")
        console.print()
        console.print(f"[dim]Note: Costs are per-model. Switch model with [bold cyan]/model[/bold cyan] to set different costs.[/dim]")
        console.print()

    else:
        console.print(f"[yellow]Cost not configured for model '{current_model}'. Set with:[/yellow]")
        console.print(f"  [bold cyan]/usage[/bold cyan] in <cost>   - Set input token cost per 1M tokens")
        console.print(f"  [bold cyan]/usage[/bold cyan] out <cost>  - Set output token cost per 1M tokens")
        console.print(f"[dim]Example: [bold cyan]/usage[/bold cyan] in 2.50[/dim]")
        console.print()

    return CommandResult(status="handled")


# Command registry - maps command names to their handlers
_COMMAND_HANDLERS = {
    "/exit": _handle_exit,
    "/quit": _handle_exit,
    "/help": _handle_help,
    "/h": _handle_help,
    "/debug": _handle_debug,
    "/compact": _handle_compact,
    "/mode": _handle_mode,
    "/logging": _handle_logging,
    "/clear": _handle_clear,
    "/new": _handle_clear,
    "/reset": _handle_clear,
    "/provider": _handle_provider,
    "/config": _handle_config,
    "/init": _handle_init,
    "/edit": _handle_edit,
    "/e": _handle_edit,
    "/usage": _handle_usage,
    "/model": _handle_model,
    "/key": _handle_key,
}


def process_command(chat_manager, user_input, console, debug_mode_container):
    """Process command and optionally return replacement content.

    Args:
        chat_manager: ChatManager instance
        user_input: User's input string
        console: Rich console for output
        debug_mode_container: Dict with 'debug' key for debug mode state

    Returns:
        tuple: (status, replacement_content)
            status: "exit" | "handled" | None
            replacement_content: str to replace user_input, or None
    """
    # Parse command and arguments
    parts = user_input.split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else None

    # Look up handler in registry
    handler = _COMMAND_HANDLERS.get(cmd)
    if handler:
        result = handler(chat_manager, console, debug_mode_container, args)
        return (result.status, result.replacement_input)
    elif cmd.startswith('/'):
        console.print(f"[red]Unknown command: {user_input}[/red]")
        console.print("[dim]Type [bold cyan]/help[/bold cyan] for available commands[/dim]")
        return ("handled", None)

    return (None, None)
