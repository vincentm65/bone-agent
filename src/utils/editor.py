"""External editor integration for vmCode."""
import os
import platform
import subprocess
import tempfile
import shutil
from pathlib import Path
from typing import Tuple, Optional


def get_editor() -> str:
    """Get editor command with OS-specific defaults.

    Priority:
    1. EDITOR environment variable
    2. OS defaults: notepad.exe (Windows) | nano/vi/vim (Unix)

    Returns:
        str: Editor command or path
    """
    # 1. Check environment variable
    editor = os.environ.get("EDITOR")
    if editor and editor.strip():
        return editor.strip()

    # 2. OS-specific defaults
    if platform.system() == "Windows":
        return "notepad.exe"
    else:
        # Try to find common editors
        for cmd in ["nvim", "nano", "vi", "vim"]:
            if shutil.which(cmd):
                return cmd
        # Fallback to nano even if not found (will error later)
        return "nano"


def _create_temp_file() -> Tuple[Path, object]:
    """Create temporary file for editing.

    Returns:
        tuple: (Path object, file handle)
    """
    # Create with .md extension for better syntax highlighting
    temp_fd = tempfile.NamedTemporaryFile(
        mode='w+',
        suffix='.md',
        prefix='vmcode_edit_',
        delete=False,
        encoding='utf-8'
    )

    temp_fd.flush()

    return Path(temp_fd.name), temp_fd


def _strip_comment_lines(content: str) -> str:
    """Remove comment lines (starting with #) from content.

    Args:
        content: Raw content from editor

    Returns:
        str: Content with comment lines removed
    """
    lines = []
    for line in content.split('\n'):
        stripped = line.strip()
        # Keep empty lines and non-comment lines
        if not stripped.startswith('#'):
            lines.append(line)

    return '\n'.join(lines).strip()


def open_editor_for_input(console, debug_mode: bool = False) -> Tuple[bool, Optional[str]]:
    """Open external editor and return user input.

    Args:
        console: Rich console for output
        debug_mode: Whether to show debug information

    Returns:
        tuple: (success: bool, content: str or None)
            - (True, content) if successful
            - (False, None) if failed or cancelled
    """
    editor_cmd = get_editor()
    temp_path = None

    try:
        # Create temporary file
        temp_path, temp_fd = _create_temp_file()
        temp_fd.close()

        if debug_mode:
            console.print(f"[dim]Temp file: {temp_path}[/dim]")
            console.print(f"[dim]Editor: {editor_cmd}[/dim]")

        console.print("[#5F9EA0]Opening editor...[/#5F9EA0]")
        console.print("[dim]Save and close the editor when done[/dim]")

        # Launch editor and wait for it to close
        # Use shell=True on Windows for notepad, False for better security on Unix
        use_shell = platform.system() == "Windows"

        result = subprocess.run(
            [editor_cmd, str(temp_path)],
            shell=use_shell,
            check=False  # Don't raise on non-zero exit
        )

        if result.returncode != 0 and debug_mode:
            console.print(f"[yellow]Editor exited with code {result.returncode}[/yellow]")

        # Read content from temp file
        content = temp_path.read_text(encoding='utf-8')

        # Strip comment lines
        content = _strip_comment_lines(content)

        if debug_mode:
            console.print(f"[dim]Read {len(content)} characters[/dim]")

        return (True, content)

    except FileNotFoundError:
        console.print(f"[red]Editor '{editor_cmd}' not found[/red]", markup=False)
        console.print("[dim]Set EDITOR as environment variable[/dim]")
        if debug_mode:
            console.print(f"[dim]Tried to run: {editor_cmd}[/dim]")
        return (False, None)

    except PermissionError as e:
        console.print(f"[red]Permission denied: {e}[/red]", markup=False)
        console.print("[dim]Check permissions on temporary directory[/dim]")
        return (False, None)

    except Exception as e:
        console.print(f"[red]Failed to open editor: {e}[/red]", markup=False)
        if debug_mode:
            import traceback
            console.print(f"[dim]{traceback.format_exc()}[/dim]")
        return (False, None)

    finally:
        # Cleanup temp file
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
                if debug_mode:
                    console.print(f"[dim]Cleaned up temp file[/dim]")
            except Exception as e:
                # Log but don't crash on cleanup failure
                console.print(f"[yellow]Warning: Failed to delete temp file: {e}[/yellow]")
                if debug_mode:
                    console.print(f"[dim]Temp file may remain at: {temp_path}[/dim]")
