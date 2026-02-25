"""Command execution via shell (PowerShell on Windows, /bin/sh on Unix/Linux) or direct execution (rg)."""

import subprocess
import shlex
import os
from pathlib import Path
from rich.panel import Panel
from llm.config import TOOLS_REQUIRE_CONFIRMATION
from utils.settings import tool_settings
from exceptions import CommandExecutionError

from .formatters import format_tool_result


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


def confirm_tool(command, console, reason=None, requires_approval=True, prompt_session=None):
    """Prompt user for tool execution confirmation.

    Args:
        command: Command to execute
        console: Rich console for output
        reason: Optional reason for requiring confirmation
        requires_approval: Whether this command specifically requires approval (overrides global flag when True)
        prompt_session: PromptSession instance for input (optional, for Linux compatibility)

    Returns:
        tuple: (action, guidance_text) where action is "execute", "reject", or "guide"
               and guidance_text contains the user's input when action is "guide"
    """
    # Skip confirmation only if: global flag is off AND command doesn't require approval
    if not TOOLS_REQUIRE_CONFIRMATION and not requires_approval:
        return ("execute", None)

    # Simple title line with tool details
    console.print("[cyan]───[/][bold white] Tool Confirmation [/][cyan]───[/]")
    if reason:
        console.print(f"Tool request: {command}")
        console.print(f"Details: {reason}")
    else:
        console.print(f"Tool request: {command}")
    console.print("[bold white]Approve tool? (y/n/guidance):[/]")

    try:
        # Use prompt_session.prompt() if available (for Linux compatibility)
        if prompt_session:
            response = prompt_session.prompt("> ").strip()
        else:
            response = input("> ").strip()
    except (EOFError, OSError):
        # stdin not available - reject command by default
        console.print("[red]User input not available - command rejected[/red]")
        return ("reject", None)

    console.print()

    if response.lower() in ("y", "yes"):
        return ("execute", None)
    elif response.lower() in ("n", "no"):
        return ("reject", None)
    else:
        return ("guide", response)


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
