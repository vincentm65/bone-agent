"""Shared prompt utilities for bone-agent CLI."""

from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from prompt_toolkit.formatted_text import HTML
from llm.config import get_provider_config, APPROVE_MODE_LABELS, STATUS_BAR_SETTINGS


def get_bottom_toolbar_text(chat_manager):
    """Return bottom toolbar text with model, approval mode, and token count.

    This is extracted from main.py for reuse in confirmation prompts.

    Args:
        chat_manager: ChatManager instance for state access

    Returns:
        HTML formatted toolbar text
    """
    provider_name = chat_manager.client.provider
    model = get_provider_config(provider_name).get("model", "Unknown")

    # Get token counts
    tokens_curr = chat_manager.token_tracker.current_context_tokens
    tokens_in = chat_manager.token_tracker.total_prompt_tokens
    tokens_out = chat_manager.token_tracker.total_completion_tokens
    tokens_total = chat_manager.token_tracker.total_tokens

    # Calculate cost — prefer upstream-reported actual cost (e.g. OpenRouter)
    # over locally estimated cost from token counts × static rates
    total_cost = chat_manager.token_tracker.get_display_cost(model)
    
    # Format model name (take last part if path)
    if "\\" in model or "/" in model:
        model_display = model.split("\\")[-1].split("/")[-1]
    else:
        model_display = model
    
    # Determine mode label and color
    mode_label = "Approval"
    val = APPROVE_MODE_LABELS.get(chat_manager.approve_mode, chat_manager.approve_mode.upper())
    colors = {"safe": "#6B8E23", "accept_edits": "#DAA520", "danger": "#CD5C5C"}
    mode_val_colored = f'<style fg="{colors.get(chat_manager.approve_mode, "white")}">{val}</style>'

    # Build toolbar string based on configuration
    # Model and mode are always shown
    parts = [f'<style fg="#606060">Model: {model_display or provider_name} - {mode_label}: </style>{mode_val_colored}']
    
    # Conditionally add token counts
    if STATUS_BAR_SETTINGS.get("show_curr_tokens", True):
        parts.append(f'<style fg="#606060"> | </style><style fg="#808080">curr</style><style fg="#606060">: {tokens_curr:,}</style>')
    if STATUS_BAR_SETTINGS.get("show_in_tokens", True):
        parts.append(f'<style fg="#606060"> | </style><style fg="#808080">in</style><style fg="#606060">: {tokens_in:,}</style>')
    if STATUS_BAR_SETTINGS.get("show_out_tokens", True):
        parts.append(f'<style fg="#606060"> | </style><style fg="#808080">out</style><style fg="#606060">: {tokens_out:,}</style>')
    if STATUS_BAR_SETTINGS.get("show_total_tokens", True):
        parts.append(f'<style fg="#606060"> | </style><style fg="#808080">total</style><style fg="#606060">: {tokens_total:,}</style>')
    
    # Conditionally add cost
    if STATUS_BAR_SETTINGS.get("show_cost", True):
        parts.append(f'<style fg="#606060"> | </style><style fg="#808080">cost</style><style fg="#606060">: ${total_cost:.4f}</style>')
    
    return HTML('\n' + ''.join(parts))


TOOLBAR_STYLE = Style.from_dict({
    "bottom-toolbar": "bg:default fg:#FFFFFF noreverse",
    "bottom-toolbar.text": "bg:default fg:#FFFFFF noreverse",
})


def setup_common_bindings(chat_manager):
    """Create KeyBindings with shared logic (e.g., Shift+Tab for mode cycling)."""
    bindings = KeyBindings()

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


def create_confirmation_prompt_session(chat_manager, message_func):
    """Create a PromptSession for confirmation prompts with key bindings and toolbar.
    
    This provides:
    - Shift+Tab to toggle approval mode
    - Bottom toolbar showing model, approval mode, and token counts
    - Dynamic prompt message that updates when mode changes
    
    Args:
        chat_manager: ChatManager instance for state access
        message_func: Function that returns the prompt message HTML (called on each redraw)
        
    Returns:
        PromptSession configured with bindings and toolbar
    """
    bindings = setup_common_bindings(chat_manager)
    
    return PromptSession(
        key_bindings=bindings,
        style=TOOLBAR_STYLE,
        bottom_toolbar=lambda: get_bottom_toolbar_text(chat_manager),
        message=message_func
    )
