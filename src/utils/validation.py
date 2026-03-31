"""Command validation and duplicate detection."""

import os
import re
import shlex
from urllib.parse import urlparse
from llm.config import ALLOWED_COMMANDS

# Localhost patterns allowed over plain HTTP (no TLS needed for loopback)
_LOCALHOST_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def validate_api_url(url: str) -> tuple[bool, str]:
    """Validate an API base URL for security.

    Enforces HTTPS for all non-localhost endpoints.
    Rejects obviously malformed URLs.

    Returns:
        (is_valid, error_message)
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, f"Malformed URL: {url}"

    if parsed.scheme not in ("http", "https"):
        return False, f"Invalid URL scheme '{parsed.scheme}', expected http or https"

    if parsed.scheme == "http" and parsed.hostname not in _LOCALHOST_HOSTS:
        return False, (
            f"Plain HTTP is not allowed for remote endpoints. "
            f"Use HTTPS for {parsed.hostname or url}"
        )

    return True, ""


# Commands that should be silently rejected in execute_command (redirect to native tools)
# These are commands that have better native tool equivalents
SILENT_COMMAND_BLOCKED = {
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

    # Additional shell commands that should use native tools
    "grep", "find", "head", "tail", "sed", "awk", "sort", "uniq", "wc",
}


def normalize_for_comparison(command):
    """Normalize command for duplicate detection."""
    # Remove shell prefix (e.g., "powershell ") and extra whitespace
    cmd = command.strip().lower()
    if cmd.startswith("powershell "):
        cmd = cmd[11:].strip()
    return cmd


def check_for_silent_blocked_command(command):
    """Check if command should be silently blocked (redirect to native tool).

    Args:
        command: Command string to validate

    Returns:
        tuple: (is_blocked, reprompt_message)
               is_blocked is True if command should be silently blocked
               reprompt_message contains guidance for the AI on what tool to use
    """
    command = command.strip()
    if not command:
        return False, None

    # Strip "powershell " prefix if present
    if command.lower().startswith("powershell "):
        command = command[len("powershell "):].strip()

    # Tokenize and get command name
    tokens = _tokenize_segment(command)
    if not tokens:
        return False, None

    cmd_name = tokens[0].lower()

    # Check if command is in the silent blocked list
    if cmd_name in SILENT_COMMAND_BLOCKED:
        tool_map = {
            "rg": "rg tool", "rg.exe": "rg tool", "ripgrep": "rg tool",
            "cat": "read_file tool", "get-content": "read_file tool", "type": "read_file tool",
            "ls": "list_directory tool", "get-childitem": "list_directory tool", "dir": "list_directory tool",
            "touch": "create_file tool", "new-item": "create_file tool",
            "set-content": "edit_file tool", "add-content": "edit_file tool", "echo": "edit_file tool", "tee": "edit_file tool",
            "grep": "rg tool for code search, or read_file tool for searching within a file",
            "find": "list_directory tool with recursive=True for listing files, or rg tool for searching content",
            "head": "read_file tool with start_line=1 and max_lines=N",
            "tail": "read_file tool with start_line and max_lines parameters",
            "sed": "edit_file tool for text replacements",
            "awk": "read_file tool followed by post-processing, or use rg tool for pattern matching",
            "sort": "read_file tool then process results",
            "uniq": "read_file tool then process results",
            "wc": "read_file tool shows line counts",
        }
        tool_suggestion = tool_map.get(cmd_name, "appropriate native tool")
        reprompt_msg = (
            f"Use the {tool_suggestion} instead of '{cmd_name}'. "
            f"Native tools provide better integration with the system."
        )
        return True, reprompt_msg

    return False, None


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


def _has_unquoted_operator(command, blocked_operators):
    """Check if command contains unquoted blocked operators.
    
    This properly handles quoted strings (e.g., 'echo "foo > bar"' should be allowed).
    
    Args:
        command: Command string to check
        blocked_operators: Tuple of operator characters to check
    
    Returns:
        bool: True if any blocked operator appears unquoted
    """
    use_posix = os.name != 'nt'
    
    # Try to tokenize - this will handle quoted strings properly
    try:
        tokens = shlex.split(command, posix=use_posix)
    except ValueError:
        # If tokenization fails, fall back to simple check (conservative)
        return any(op in command for op in blocked_operators)
    
    # Check if any token exactly matches a blocked operator
    # These operators are parsed as separate tokens by the shell
    for token in tokens:
        if token in blocked_operators:
            return True
    
    # Also check for operators attached to arguments (e.g., "file>out" or ">file")
    # These are dangerous even if shlex doesn't separate them as distinct tokens
    for token in tokens:
        for op in blocked_operators:
            if op in token and token != op:
                # Check if operator is at the start or end (attachment)
                if token.startswith(op) or token.endswith(op):
                    return True
    
    return False


def _parse_pipe_chain(command):
    """Parse a command string into individual commands in a pipe chain.
    
    Args:
        command: Command string that may contain pipe operators
        
    Returns:
        list: List of individual command strings
    """
    # Strip whitespace
    command = command.strip()
    if not command:
        return []
    
    use_posix = os.name != 'nt'
    commands = []
    current_cmd = []
    
    try:
        # Tokenize to handle quoted strings properly
        tokens = shlex.split(command, posix=use_posix)
        
        # Split on pipe operator
        for token in tokens:
            if token == '|':
                # End current command and start new one
                if current_cmd:
                    commands.append(shlex.join(current_cmd))
                    current_cmd = []
            else:
                current_cmd.append(token)
        
        # Add final command
        if current_cmd:
            commands.append(shlex.join(current_cmd))
        
        return commands
    except ValueError:
        # If tokenization fails, fall back to simple split
        # This is less accurate but still functional
        return [cmd.strip() for cmd in command.split('|') if cmd.strip()]


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
    # Allow | for piping, but each command in the pipe chain will be validated separately
    blocked_operators = (";", ">", "<", "`")
    if _has_unquoted_operator(command, blocked_operators):
        return False, "contains disallowed shell operators"
    
    # Note: && is allowed for conditional chaining. | is allowed for piping.
    # The agent will display a warning in debug mode when using && or | for multi-step commands.

    # After stripping prefix, reject if it still starts with "powershell"
    if command.lower().startswith("powershell"):
        return False, "nested powershell invocation"

    # Parse pipe chain and validate each command
    pipe_commands = _parse_pipe_chain(command)
    
    if len(pipe_commands) > 1:
        # We have a pipe chain - validate each command separately
        for i, cmd in enumerate(pipe_commands):
            # Check each command against silent blocked list
            is_blocked, reprompt_msg = check_for_silent_blocked_command(cmd)
            if is_blocked:
                # Modify the reprompt message to indicate which command in the pipe chain
                return False, f"Pipe chain blocked: command {i+1} uses '{cmd.split()[0]}' - {reprompt_msg}"
            
            # Validate each command has a valid command name
            tokens = _tokenize_segment(cmd)
            if not tokens:
                return False, f"Pipe chain blocked: command {i+1} is empty"
            
            # All commands in pipe chain must be valid (not in silent blocked list)
            # They still go through normal approval process
    else:
        # Single command - validate normally
        tokens = _tokenize_segment(command)
        if not tokens:
            return False, "empty command"

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
