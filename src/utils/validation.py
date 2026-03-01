"""Command validation and duplicate detection."""

import os
import shlex
from llm.config import ALLOWED_COMMANDS


# Commands that overlap with native tools (blocked - use the tool instead)
BLOCKED_OVERLAPS = {
    # Code search (use rg tool)
    "rg", "rg.exe", "ripgrep",
    
    # File reading (use read_file tool)
    "cat", "get-content", "type",
    
    # Directory listing (use list_directory tool)
    "ls", "get-childitem", "dir",
    
    # File creation (use create_file tool)
    "touch", "new-item",
    
    # File editing (use edit_file tool)
    "set-content", "add-content", "echo", "tee",
}


def normalize_for_comparison(command):
    """Normalize command for duplicate detection."""
    # Remove shell prefix (e.g., "powershell ") and extra whitespace
    cmd = command.strip().lower()
    if cmd.startswith("powershell "):
        cmd = cmd[11:].strip()
    return cmd


def check_for_duplicate(chat_manager, command):
    """Check if command was already run and return simple warning.

    Returns:
        tuple: (is_duplicate, redirect_message)
    """
    normalized = normalize_for_comparison(command)

    if normalized in chat_manager.command_history:
        redirect_msg = (
            f"exit_code=DUPLICATE\n"
            f"This exact command was already executed: {command}\n\n"
            f"The result is already in your conversation history.\n"
            f"Try a DIFFERENT command with different search terms, "
            f"different file paths, or a different approach entirely."
        )
        return True, redirect_msg

    # Add to history
    chat_manager.command_history.append(normalized)
    return False, None


def _tokenize_segment(segment):
    use_posix = os.name != "nt"
    try:
        return shlex.split(segment, posix=use_posix)
    except ValueError:
        return segment.split()


def check_command(command):
    """Validate command against safety checks.

    Args:
        command: Command string to validate

    Returns:
        tuple: (is_safe, reason) - is_safe is True if command is allowed
               If is_safe is True, the caller should check approval requirements separately
    """
    command = command.strip()
    if not command:
        return False, "empty command"

    # Strip "powershell " prefix if present (legacy support for Windows users)
    if command.lower().startswith("powershell "):
        command = command[len("powershell "):].strip()

    # Block dangerous operators (command chaining and redirection)
    # Allow && for conditional chaining (stops on error - safer than ; or &)
    blocked_operators = (";", ">", "<", "`", "|")
    if any(token in command for token in blocked_operators):
        return False, "contains disallowed shell operators"
    
    # Note: && is allowed for conditional chaining. The agent will display
    # a warning in debug mode when using && for multi-step commands.

    # After stripping prefix, reject if it still starts with "powershell"
    if command.lower().startswith("powershell"):
        return False, "nested powershell invocation"

    # Tokenize and validate command name
    tokens = _tokenize_segment(command)
    if not tokens:
        return False, "empty command"

    cmd_name = tokens[0].lower()

    # Block commands that overlap with native tools
    if cmd_name in BLOCKED_OVERLAPS:
        tool_map = {
            "rg": "rg tool", "rg.exe": "rg tool", "ripgrep": "rg tool",
            "cat": "read_file tool", "get-content": "read_file tool", "type": "read_file tool",
            "ls": "list_directory tool", "get-childitem": "list_directory tool", "dir": "list_directory tool",
            "touch": "create_file tool", "new-item": "create_file tool",
            "set-content": "edit_file tool", "add-content": "edit_file tool", "echo": "edit_file tool", "tee": "edit_file tool",
        }
        tool_suggestion = tool_map.get(cmd_name, "appropriate native tool")
        return False, f"command '{cmd_name}' overlaps with {tool_suggestion}. Use the native tool instead."

    # Allow all other commands
    return True, None


def is_auto_approved_command(command):
    """Check if a command should be auto-approved (safe, read-only commands).

    Args:
        command: Command string to validate

    Returns:
        bool: True if command is in ALLOWED_COMMANDS list (auto-approved)
    """
    command = command.strip()
    if not command:
        return False

    # Strip "powershell " prefix if present (legacy support for Windows users)
    if command.lower().startswith("powershell "):
        command = command[len("powershell "):].strip()

    # Tokenize and get command name
    tokens = _tokenize_segment(command)
    if not tokens:
        return False

    cmd_name = tokens[0].lower()

    # Check if command is in the auto-approved list
    return cmd_name in ALLOWED_COMMANDS
