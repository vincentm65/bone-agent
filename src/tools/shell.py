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

    # Handle case where console is None (e.g., parallel execution)
    if console is None:
        # Reject by default when console is unavailable
        return ("reject", None)

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
        if console is not None:
            console.print("[red]User input not available - command rejected[/red]")
        return ("reject", None)

    if console is not None:
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


# =============================================================================
# @tool decorated function
# =============================================================================

@tool(
    name="execute_command",
    description="Execute shell commands for git, system tasks, debugging, and file operations.\n\n**Use for:**\n- Git operations: git clone, pull, push, status, etc.\n- System debugging: ps, lsof, netstat, journalctl, systemctl\n- File operations: rm, mv, cp, mkdir (system-wide)\n- Network tools: ping, curl, wget, ssh\n- Package management: pacman, pip, npm, apt\n- Path navigation: cd /path && command (use && for chaining)\n\n**Important:**\n- All commands execute from repository root\n- Use && for conditional chaining (stops on error)\n- Absolute paths allowed for system debugging\n\n**Do NOT use for:**\n- Code search (use rg tool)\n- Reading files (use read_file)\n- Listing directories (use list_directory)\n- Creating/editing files (use create_file/edit_file)\n- NO chaining with ;, |, >, <, ` (only && allowed)",
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Command to execute. Examples: 'git status', 'ps aux', 'cd /var/log && tail -f syslog'"
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
    gitignore_spec = None
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
    from utils.validation import check_for_duplicate, check_command

    if not isinstance(command, str) or not command.strip():
        return "exit_code=1\nerror: 'command' argument must be a non-empty string."

    # Check for duplicates
    is_duplicate, redirect_msg = check_for_duplicate(chat_manager, command)
    if is_duplicate:
        return redirect_msg

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
