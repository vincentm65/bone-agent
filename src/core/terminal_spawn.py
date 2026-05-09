"""Cross-platform terminal window spawner for bone-agent swarm workers."""

import logging
import os
import platform
import shlex
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Per-terminal argument prefix used before the worker command on Linux.
_TERMINAL_PREFIXES = {
    "gnome-terminal": ["--"],
    "konsole": ["-e"],
    "xfce4-terminal": ["-e"],
    "alacritty": ["-e"],
    "lxterminal": ["-e"],
    "mate-terminal": ["-e"],
    "sakura": ["-e"],
    "st": ["-e"],
    "urxvt": ["-e"],
    "terminator": ["-e"],
    "tilix": ["-e"],
    "wezterm": ["start", "--"],
    "kitty": [],
    "foot": [],
    "footclient": [],
    "xterm": ["-e"],
}


def _escape_applescript(s: str) -> str:
    """Escape double quotes in a string for AppleScript."""
    return s.replace('"', '\\"')


def _shell_quote(s: str) -> str:
    """Quote a string for safe use in a shell command (POSIX)."""
    import shlex
    return shlex.quote(s)


def _spawn_macos(command: str, cwd: str) -> subprocess.Popen:
    """Spawn a terminal window on macOS using osascript."""
    cmd_escaped = _escape_applescript(command)
    cwd_quoted = _shell_quote(cwd)
    script = f'tell app "Terminal" to do script "cd {cwd_quoted} && {cmd_escaped}"'
    logger.info("Spawning macOS Terminal.app window")
    return subprocess.Popen(["osascript", "-e", script])


def _spawn_windows(command: str, cwd: str) -> subprocess.Popen:
    """Spawn a terminal window on Windows using cmd.exe."""
    wrapped_cmd = f'cd /d "{cwd}" && {command}'
    logger.info("Spawning Windows cmd.exe window")
    return subprocess.Popen(f'start cmd /k "{wrapped_cmd}"', shell=True)


def _spawn_wsl(command: str, cwd: str) -> subprocess.Popen:
    """Spawn a terminal on WSL by launching Windows cmd."""
    wrapped_cmd = f'cd /d "{cwd}" && {command}'
    logger.info("Spawning WSL terminal via Windows cmd")
    return subprocess.Popen(
        f'start cmd /k "{wrapped_cmd}"',
        shell=True,
    )


def _wrap_posix_worker_command(command: str, cwd: str) -> str:
    """Build a shell script that keeps worker startup failures visible."""
    return "\n".join(
        [
            f"cd {shlex.quote(cwd)} || exit $?",
            "printf '\\033]0;bone worker\\007' 2>/dev/null || true",
            command,
            "status=$?",
            'if [ "$status" -ne 0 ]; then',
            "  printf '\\nWorker command exited with status %s.\\n' \"$status\"",
            "  printf 'Command: %s\\n' " + shlex.quote(command),
            "  printf 'Working directory: %s\\n' " + shlex.quote(cwd),
            "  printf 'Press Enter to close this worker terminal... '",
            "  read -r _ </dev/tty || true",
            "fi",
            'exit "$status"',
        ]
    )


def _shell_login_argv(script: str) -> list[str]:
    """Return argv for running a script in a login-ish shell."""
    return ["bash", "-lc", script]


def _has_display() -> bool:
    """Check if a display server or terminal multiplexer is available on Linux."""
    return bool(
        os.environ.get("DISPLAY")
        or os.environ.get("WAYLAND_DISPLAY")
        or os.environ.get("TMUX")
        or os.environ.get("STY")
    )


def _terminal_basename(term_cmd: list[str]) -> str:
    """Return the executable basename for a terminal command."""
    return os.path.basename(term_cmd[0])


def _is_wayland_terminal(term_cmd: str | list[str]) -> bool:
    """Return True for terminal emulators that require a Wayland display."""
    parts = shlex.split(term_cmd) if isinstance(term_cmd, str) else list(term_cmd)
    if not parts:
        return False
    return _terminal_basename(parts) in {"foot", "footclient"}


def _build_terminal_argv(term_cmd: str | list[str], script: str) -> list[str]:
    """Build argv for a Linux terminal emulator.

    ``term_cmd`` may be a plain executable name, an absolute path, or a
    command with arguments from ``$TERMINAL`` such as ``"footclient --server"``.
    """
    parts = shlex.split(term_cmd) if isinstance(term_cmd, str) else list(term_cmd)
    if not parts:
        raise RuntimeError("Terminal command must not be empty")

    term = _terminal_basename(parts)
    prefix = _TERMINAL_PREFIXES.get(term, ["-e"])
    return [*parts, *prefix, *_shell_login_argv(script)]


def _raise_if_launcher_failed(process: subprocess.Popen, label: str) -> None:
    """Raise when the terminal launcher itself exits immediately with failure."""
    try:
        return_code = process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        return

    if return_code != 0:
        raise RuntimeError(f"{label} exited immediately with status {return_code}")


def _popen_launcher(argv: list[str], cwd: str | None = None) -> subprocess.Popen:
    """Launch a terminal emulator without leaking launcher diagnostics."""
    kwargs = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if cwd is not None:
        kwargs["cwd"] = cwd
    return subprocess.Popen(argv, **kwargs)


def _spawn_linux(command: str, cwd: str) -> subprocess.Popen:
    """Spawn a terminal on Linux, trying methods in priority order."""
    if not _has_display():
        raise RuntimeError(
            "No display server or tmux detected. "
            "Cannot open terminal window."
        )

    script = _wrap_posix_worker_command(command, cwd)
    launcher_errors: list[str] = []

    # 1. tmux — if already inside tmux, create a new window
    if os.environ.get("TMUX"):
        logger.info("Spawning via tmux new-window")
        process = _popen_launcher(["tmux", "new-window", "-c", cwd, "bash -lc " + shlex.quote(script)])
        _raise_if_launcher_failed(process, "tmux new-window")
        return process

    # 2. screen — if already inside screen, create a new session
    if os.environ.get("STY"):
        logger.info("Spawning via screen")
        process = _popen_launcher(["screen", "-t", "worker", *_shell_login_argv(script)])
        _raise_if_launcher_failed(process, "screen")
        return process

    # 3. $TERMINAL env var
    term_cmd = os.environ.get("TERMINAL")
    if term_cmd:
        logger.info("Spawning via $TERMINAL (%s)", term_cmd)
        if _is_wayland_terminal(term_cmd) and not os.environ.get("WAYLAND_DISPLAY"):
            launcher_errors.append(f"{term_cmd}: WAYLAND_DISPLAY is not set")
        else:
            try:
                process = _popen_launcher(_build_terminal_argv(term_cmd, script), cwd=cwd)
                _raise_if_launcher_failed(process, term_cmd)
                return process
            except Exception as e:
                launcher_errors.append(f"{term_cmd}: {e}")
                logger.warning("$TERMINAL failed, trying next method: %s", e)

    # 4. Probe PATH for known terminals before desktop helpers. Preferred
    # terminal helpers can be installed but misconfigured, especially on XFCE.
    for term in _TERMINAL_PREFIXES:
        if term in {"foot", "footclient"} and not os.environ.get("WAYLAND_DISPLAY"):
            continue
        found = shutil.which(term)
        if found:
            logger.info("Spawning via %s", term)
            try:
                process = _popen_launcher(_build_terminal_argv([found], script), cwd=cwd)
                _raise_if_launcher_failed(process, term)
                return process
            except Exception as e:
                launcher_errors.append(f"{term}: {e}")
                logger.warning("%s failed, trying next terminal: %s", term, e)

    # 5. xdg-terminal-exec
    xdg = shutil.which("xdg-terminal-exec")
    if xdg:
        logger.info("Spawning via xdg-terminal-exec")
        try:
            process = _popen_launcher([xdg, *_shell_login_argv(script)], cwd=cwd)
            _raise_if_launcher_failed(process, "xdg-terminal-exec")
            return process
        except Exception as e:
            launcher_errors.append(f"xdg-terminal-exec: {e}")
            logger.warning("xdg-terminal-exec failed, trying next method: %s", e)

    # 6. Desktop helpers
    if shutil.which("exo-open"):
        try:
            result = _popen_launcher(
                ["exo-open", "--launch", "TerminalEmulator", *_shell_login_argv(script)],
                cwd=cwd,
            )
            logger.info("Spawning via exo-open (XFCE)")
            _raise_if_launcher_failed(result, "exo-open")
            return result
        except Exception as e:
            launcher_errors.append(f"exo-open: {e}")
            logger.warning("exo-open failed, trying next method: %s", e)

    message = (
        "No supported terminal emulator could be launched. "
        "Set $TERMINAL to a working terminal command or install one of the supported terminals."
    )
    if launcher_errors:
        message += " Tried: " + "; ".join(launcher_errors)
    raise RuntimeError(message)


def spawn_terminal(command: str, cwd: str) -> subprocess.Popen:
    """Open a new terminal window and run command.

    Args:
        command: The shell command to run in the new terminal.
        cwd: Working directory for the new process.

    Returns:
        The subprocess.Popen object for the terminal process.

    Raises:
        RuntimeError: If no terminal or display method is available.
    """
    release = platform.uname().release.lower()
    if "microsoft" in release:
        return _spawn_wsl(command, cwd)

    system = platform.system()
    if system == "Darwin":
        return _spawn_macos(command, cwd)
    elif system == "Windows":
        return _spawn_windows(command, cwd)
    elif system == "Linux":
        return _spawn_linux(command, cwd)

    raise RuntimeError(
        f"Unsupported platform: {system}. "
        "Only Linux, macOS, and Windows are supported."
    )


def build_and_spawn_workers(
    server,
    count: int,
    profile: str = "",
    cwd: str | None = None,
    confirm: "Callable[[int], bool] | None" = None,
) -> tuple[int, list[str]]:
    """Build the bone-agent worker CLI command and spawn terminals.

    Args:
        server: SwarmServer instance (must have get_spawn_info()).
        count: Number of worker terminals to spawn (1-10, or more with confirm).
        profile: Optional worker profile name.
        cwd: Working directory for spawned terminals. Defaults to server's repo_root.
        confirm: Optional callback invoked with count when count > 10.
            Return True to proceed, False to abort. If not provided, raises ValueError.

    Returns:
        Tuple of (spawned_count, list_of_error_messages).

    Raises:
        ValueError: If count is out of range or confirmation denied.
        RuntimeError: If no display/terminal is available.
    """
    if count < 1:
        raise ValueError("count must be at least 1")
    if count > 10:
        if confirm is None or not confirm(count):
            raise ValueError(
                f"Spawning {count} workers requires confirmation. "
                "Approve the request to proceed."
            )

    info = server.get_spawn_info()

    # Resolve paths relative to this module's location at runtime.
    # Works for both npm installs and source installs.
    _src_dir = Path(__file__).resolve().parent.parent  # src/
    _main_py = _src_dir / "ui" / "main.py"

    # Build command with proper shell escaping.
    # Use the full resolved command (alias expansion) because spawned
    # terminals are non-interactive and .bashrc exits before aliases load.
    cmd_parts = [
        f"python {shlex.quote(str(_main_py))}",
        "--worker", shlex.quote(info["swarm_name"]),
        "--swarm-host", shlex.quote(info["host"]),
        "--swarm-port", shlex.quote(str(info["port"])),
        "--auth-token", shlex.quote(str(info["auth_token"])),
    ]
    if profile and profile.strip():
        cmd_parts.extend(["--profile", shlex.quote(profile)])

    command = " ".join(cmd_parts)

    if cwd is None:
        cwd = info.get("repo_root") or os.getcwd()

    spawned = 0
    errors = []
    for _ in range(count):
        try:
            spawn_terminal(command, cwd=cwd)
            spawned += 1
        except RuntimeError:
            raise  # no display — stop immediately, all will fail
        except Exception as e:
            errors.append(str(e))

    return spawned, errors
