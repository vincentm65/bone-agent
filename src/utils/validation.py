"""Command validation."""

import os
import re
import shlex
from urllib.parse import urlparse

# Shell operators that indicate command chaining or redirection.
# Shared between validation.py and shell.py — keep in one place to avoid drift.
# Matches: &&, ||, ;, |, >, <, backticks, $(), ${}, newlines
# NOTE: Alternations are sorted longest-first so that '&&' and '||' match
# before '|' — reordering the raw list is safe because we sort at runtime.
_RAW_CHAINING_PATTERNS = ["&&", "||", ";", "|", ">", "<", "`", "$(", "${", "\n", "\r"]
CHAINING_OPERATORS = re.compile(
    "|".join(re.escape(p) for p in sorted(_RAW_CHAINING_PATTERNS, key=len, reverse=True))
)

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

    # For chained commands, only skip silent blocking if the FIRST command
    # is not a blocked tool. e.g. "cd /var/log && tail -f" is allowed, but
    # "cat file && echo done" is still redirected to read_file.
    if CHAINING_OPERATORS.search(command):
        first_segment = CHAINING_OPERATORS.split(command, maxsplit=1)[0].strip()
        first_tokens = _tokenize_segment(first_segment)
        if first_tokens and first_tokens[0].lower() not in SILENT_COMMAND_BLOCKED:
            return False, None
        # else: fall through to blocked check below

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



def _tokenize_segment(segment):
    use_posix = os.name != "nt"
    try:
        return shlex.split(segment, posix=use_posix)
    except ValueError:
        return segment.split()


def check_command(command):
    """Perform basic structural validation on a command.

    Rejects empty commands and nested powershell invocations.
    Approval and safety checks are handled upstream by the caller.

    Args:
        command: Command string to validate

    Returns:
        tuple: (is_valid, reason) - is_valid is True if the command
               has a non-empty structure. reason is set on rejection.
    """
    command = command.strip()
    if not command:
        return False, "empty command"

    # Strip "powershell " prefix if present (legacy support for Windows users)
    if command.lower().startswith("powershell "):
        command = command[len("powershell "):].strip()

    # After stripping prefix, reject if it still starts with "powershell"
    if command.lower().startswith("powershell"):
        return False, "nested powershell invocation"

    # Multi-line shell scripts/heredocs are valid command payloads.
    # Do not tokenize the full script here: tokenization can reject valid shell
    # syntax before the shell sees it. Safety/approval checks happen upstream.
    if "\n" in command:
        for line in command.splitlines():
            line = line.strip()
            if line and line.lower().startswith("powershell"):
                return False, "nested powershell invocation"
        return True, None

    # Basic validation - ensure command has content
    tokens = _tokenize_segment(command)
    if not tokens:
        return False, "empty command"

    # Allow all other commands
    return True, None


def is_auto_approved_command(command):
    """Check if a command should be auto-approved (safe, read-only commands).

    Delegates to the structured safety system in utils.safe_commands.

    Args:
        command: Command string to validate

    Returns:
        bool: True if command is safe to auto-approve
    """
    from utils.safe_commands import is_safe_command
    return is_safe_command(command)
