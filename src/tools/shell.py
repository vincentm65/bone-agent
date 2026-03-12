"""Shell command execution tool with core command execution logic and @tool decorator."""

import subprocess
import shlex
import os
from pathlib import Path
from typing import Optional
from rich.panel import Panel
from llm.config import TOOLS_REQUIRE_CONFIRMATION
from utils.settings import tool_settings
from exceptions import CommandExecutionError

from .helpers.base import tool
from .helpers.formatters import format_tool_result


def normalize_command(command, rg_exe_path):
    """Parse command and return (executable, args_list, needs_shell).

    Returns:
        tuple: (executable_path, args_list, needs_shell)
            - executable_path: Path object for rg.exe, or None for shell commands
            - args_list: List of arguments for direct execution, or command string for shell
            - needs_shell: Boolean indicating if command should run through shell
    """
    command = command.strip()

    # Handle rg commands
    if command.startswith("rg ") or command == "rg":
        if command == "rg":
            return rg_exe_path, [], False

        args_str = command[3:].strip()  # Everything after "rg "
        # Parse only the arguments, not the full command string.
        # On Windows, posix=False preserves backslashes in paths.
        use_posix = os.name != "nt"
        args = shlex.split(args_str, posix=use_posix) if args_str else []
        args = [arg[1:-1] if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in ("'", '"') else arg for arg in args]
        return rg_exe_path, args, False

    # Other commands go through shell
    return None, command, True


def confirm_tool(command, console, reason=None, requires_approval=True, approve_mode="safe", use_panel=True, is_edit_tool=False, cycle_approve_mode=None):
    """Prompt user for tool execution confirmation.

    Args:
        command: Command to execute
        console: Rich console for output (currently unused, kept for compatibility)
        reason: Optional reason for requiring confirmation
        requires_approval: Whether this command specifically requires approval (overrides global flag when True)
        approve_mode: Approval mode setting - "safe" requires confirmation, "accept_edits" auto-approves edits
        use_panel: When True, display interactive confirmation panel. When False, auto-cancel without UI (default: True)
        is_edit_tool: Whether this is an edit tool (shows extra toggle option in panel)
        cycle_approve_mode: Optional callback to cycle approve_mode

    Returns:
        tuple: (action, guidance_text) where action is "accept", "advise", or "cancel"
               and guidance_text contains the user's input when action is "advise"
    """
    # Skip confirmation only if: global flag is off AND command doesn't require approval
    if not TOOLS_REQUIRE_CONFIRMATION and not requires_approval:
        return ("accept", None)

    # Skip confirmation for edit operations in accept_edits mode
    # This only applies to file edits, not execute_command
    if approve_mode == "accept_edits" and requires_approval and "edit_file" in command:
        return ("accept", None)

    # Handle case where console is None (e.g., parallel execution)
    if console is None:
        # Cancel by default when console is unavailable
        return ("cancel", None)

    # Try to use interactive panel
    if use_panel:
        from ui.tool_confirmation import ToolConfirmationPanel
        panel = ToolConfirmationPanel(command, reason, is_edit_tool=is_edit_tool, cycle_approve_mode=cycle_approve_mode)
        return panel.run()

    # Cancel by default if panel is disabled or fails
    return ("cancel", None)


def _prepare_execution_environment(repo_root, rg_exe_path):
    """Prepare environment variables for command execution.

    Returns:
        dict: Environment variables with updated PATH
    """
    env = os.environ.copy()
    rg_parent = Path(rg_exe_path).parent if rg_exe_path else None

    if rg_parent and rg_parent.exists():
        bin_path = str(rg_parent)
    else:
        bin_path = str(repo_root / "bin")

    env["PATH"] = f"{bin_path}{os.pathsep}{env.get('PATH', '')}"
    return env


def _execute_direct_command(cmd_list, repo_root, env, debug_mode, console):
    """Execute command directly (rg.exe) without PowerShell.

    Returns:
        subprocess.CompletedProcess
    """
    if debug_mode and console:
        console.print(f"[dim]→ Executing: {cmd_list}[/dim]")
        console.print(f"[dim]→ Working dir: {repo_root}[/dim]")

    result = subprocess.run(
        cmd_list,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=tool_settings.command_timeout_sec,
        cwd=str(repo_root),
        env=env,
    )

    if debug_mode and console:
        console.print(f"[dim]→ Exit code: {result.returncode}[/dim]")

    return result


def _execute_shell_command(command, repo_root, env, debug_mode, console):
    """Execute command via shell (PowerShell on Windows, /bin/sh on Unix/Linux).

    Returns:
        subprocess.CompletedProcess
    """
    # Detect platform and use appropriate shell
    is_windows = os.name == 'nt'

    if is_windows:
        shell_cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", str(command)]
        shell_name = "PowerShell"
    else:
        shell_cmd = ["/bin/sh", "-c", str(command)]
        shell_name = "/bin/sh"

    if debug_mode and console:
        console.print(f"[dim]→ Executing via {shell_name}: {command}[/dim]")
        console.print(f"[dim]→ Working dir: {repo_root}[/dim]")

    result = subprocess.run(
        shell_cmd,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=tool_settings.command_timeout_sec,
        cwd=str(repo_root),
        env=env,
    )

    if debug_mode and console:
        console.print(f"[dim]→ Exit code: {result.returncode}[/dim]")

    return result


def run_shell_command(command, repo_root, rg_exe_path, console, debug_mode, gitignore_spec=None):
    """Execute command via rg (direct) or shell (PowerShell on Windows, /bin/sh on Unix/Linux).

    Args:
        command: Command string to execute
        repo_root: Path to repository root
        rg_exe_path: Path to rg.exe
        console: Rich console for output
        debug_mode: Whether to show debug output

    Returns:
        str: Formatted tool result

    Raises:
        CommandExecutionError: If command execution fails
    """
    try:
        env = _prepare_execution_environment(repo_root, rg_exe_path)
        executable, args, needs_shell = normalize_command(command, rg_exe_path)

        if not needs_shell:
            # Direct execution (rg)
            cmd_list = [str(executable)] + args
            result = _execute_direct_command(cmd_list, repo_root, env, debug_mode, console)
            # AI gets truncated results (via format_tool_result); user sees summary via _display_tool_feedback
            formatted_result = format_tool_result(result, command=command, is_rg=True, debug_mode=True)
        else:
            # Shell execution (PowerShell on Windows, /bin/sh on Unix/Linux)
            result = _execute_shell_command(args, repo_root, env, debug_mode, console)
            # AI gets full results; user sees summary via _display_tool_feedback
            formatted_result = format_tool_result(result, command=command, debug_mode=True)

        if debug_mode and console:
            console.print()
            console.print(f"[dim]→ AI receives:\n{formatted_result}[/dim]")

        return formatted_result
    except CommandExecutionError:
        # Re-raise our custom exceptions
        raise
    except Exception as exc:
        raise CommandExecutionError(
            f"Command execution failed",
            details={"command": command, "original_error": str(exc)}
        )


# =============================================================================
# @tool decorated function
# =============================================================================

@tool(
    name="execute_command",
    description="Execute shell commands for git, system debugging, file operations, network tools, package management, and path navigation. Commands run from repository root. Chaining: && (conditional), | (pipe) allowed. Use for git, ps, lsof, netstat, journalctl, systemctl, rm, mv, cp, mkdir, ping, curl, wget, ssh, pacman, pip, npm, apt. Disallowed commands: rg, cat, ls, grep, find, head, tail, sed, awk, sort, uniq, wc, echo, touch, get-content, type, get-childitem, dir, new-item, set-content, add-content, tee. Use native tools instead. NO chaining with ;, >, <, `.",
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Command to execute. Examples: 'git status', 'ps aux', 'cd /var/log && tail -f syslog'"
            },
            "reason": {
                "type": "string",
                "description": "Brief explanation of why this command is needed (shown during confirmation)"
            }
        },
        "required": ["command"]
    },
    allowed_modes=["edit", "plan", "learn"],
    requires_approval=True
)
def execute_command(
    command: str,
    repo_root: Path,
    rg_exe_path: str,
    console,
    chat_manager,
    debug_mode: bool = False,
    gitignore_spec = None,
    reason: str = None
) -> str:
    """Execute a shell command.

    Args:
        command: Command string to execute
        repo_root: Repository root directory (injected by context)
        rg_exe_path: Path to rg executable (injected by context)
        console: Rich console for output (injected by context)
        chat_manager: ChatManager instance (injected by context)
        debug_mode: Whether debug mode is enabled (injected by context)
        gitignore_spec: PathSpec for .gitignore filtering (injected by context)

    Returns:
        Command output with exit code
    """
    # Import validation functions here to avoid circular dependency
    from utils.validation import check_command, check_for_silent_blocked_command

    if not isinstance(command, str) or not command.strip():
        return "exit_code=1\nerror: 'command' argument must be a non-empty string."

    # Check for commands that should use native tools instead (silent blocking)
    is_blocked, reprompt_msg = check_for_silent_blocked_command(command)
    if is_blocked:
        # Return reprompt message to guide the AI to use the native tool
        # This is not shown to the user - the AI sees it and can retry
        if debug_mode and console:
            console.print(f"[dim]Silently blocked command: {command.split()[0]}[/dim]")
        return f"exit_code=1\n{reprompt_msg}"

    # Validate command
    is_safe, reason = check_command(command)
    if not is_safe:
        return reason

    # Execute command (approval workflow handled by orchestrator)
    try:
        return run_shell_command(
            command, repo_root, rg_exe_path, console, debug_mode, gitignore_spec
        )
    except Exception as e:
        return f"exit_code=1\nCommand execution failed: {str(e)}"
