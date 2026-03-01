"""Ripgrep search tool."""

import shlex
from pathlib import Path
from typing import Optional

from .helpers.base import tool
from .shell import run_shell_command
from utils.validation import check_for_duplicate
from .helpers.converters import coerce_bool, coerce_int


@tool(
    name="rg",
    description="A powerful search tool built on ripgrep. Works on any directory in the filesystem.\n\n**Usage:**\n- ALWAYS use rg for search tasks. NEVER invoke `grep` or `rg` as a shell command. The rg tool has been optimized for correct permissions and access.\n- Supports full regex syntax (e.g., \"log.*Error\", \"function\\s+\\w+\")\n- Filter files with glob parameter (e.g., \"*.js\", \"**/*.tsx\") or type parameter (e.g., \"js\", \"py\", \"rust\")\n- Output modes: \"content\" shows matching lines, \"files_with_matches\" shows only file paths (default), \"count\" shows match counts\n- Use sub_agent tool for open-ended searches requiring multiple rounds\n- Pattern syntax: Uses ripgrep (not grep) - literal braces need escaping (use `interface\\{\\}` to find `interface{}` in Go code)\n- Multiline matching: By default patterns match within single lines only. For cross-line patterns like `struct \\{[\\s\\S]*?field`, use `multiline: true`",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "The regular expression pattern to search for in file contents"
            },
            "path": {
                "type": "string",
                "description": "File or directory to search in (rg PATH). Defaults to current working directory. Works anywhere in the filesystem."
            },
            "glob": {
                "type": "string",
                "description": "Glob pattern to filter files (e.g. \"*.js\", \"*.{ts,tsx}\") - maps to rg --glob"
            },
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_with_matches", "count"],
                "description": "Output mode: \"content\" shows matching lines (supports -B/-A/-C context, -n line numbers), \"files_with_matches\" shows file paths, \"count\" shows match counts. Defaults to \"files_with_matches\"."
            },
            "-B": {
                "type": "number",
                "description": "Number of lines to show before each match (rg -B). Requires output_mode: \"content\", ignored otherwise."
            },
            "-A": {
                "type": "number",
                "description": "Number of lines to show after each match (rg -A). Requires output_mode: \"content\", ignored otherwise."
            },
            "-C": {
                "type": "number",
                "description": "Number of lines to show before and after each match (rg -C). Requires output_mode: \"content\", ignored otherwise."
            },
            "-n": {
                "type": "boolean",
                "description": "Show line numbers in output (rg -n). Requires output_mode: \"content\", ignored otherwise."
            },
            "-i": {
                "type": "boolean",
                "description": "Case insensitive search (rg -i)"
            },
            "type": {
                "type": "string",
                "description": "File type to search (rg --type). Common types: js, py, rust, go, java, etc. More efficient than include for standard file types."
            },
            "multiline": {
                "type": "boolean",
                "description": "Enable multiline mode where . matches newlines and patterns can span lines (rg -U --multiline-dotall). Default: false."
            }
        },
        "required": ["pattern"]
    },
    allowed_modes=["edit", "plan", "learn"],
    requires_approval=False
)
def rg(
    pattern: str,
    repo_root: Path,
    rg_exe_path: str,
    console,
    chat_manager,
    debug_mode: bool = False,
    gitignore_spec = None,
    path: Optional[str] = None,
    glob: Optional[str] = None,
    output_mode: str = "files_with_matches",
    **kwargs
) -> str:
    """Search for patterns using ripgrep.

    Args:
        pattern: Regular expression pattern to search for
        repo_root: Repository root directory (injected by context)
        rg_exe_path: Path to rg executable (injected by context)
        console: Rich console for output (injected by context)
        chat_manager: ChatManager instance (injected by context)
        debug_mode: Whether debug mode is enabled (injected by context)
        gitignore_spec: PathSpec for .gitignore filtering (injected by context)
        path: File or directory to search in (default: current directory)
        glob: Glob pattern to filter files
        output_mode: Output mode (content/files_with_matches/count)
        **kwargs: Additional keyword arguments (-B, -A, -C, -n, -i, type, multiline)

    Returns:
        Search results with exit code
    """
    if not isinstance(pattern, str) or not pattern.strip():
        return "exit_code=1\nrg requires a non-empty 'pattern' argument."

    # Build rg command from arguments
    cmd_parts = ["rg"]

    # Add --line-number for content mode
    if output_mode == "content":
        cmd_parts.append("--line-number")

    # Add multiline flag
    multiline = coerce_bool(kwargs.get("multiline"), default=False)
    if multiline:
        cmd_parts.append("-U")
        cmd_parts.append("--multiline-dotall")

    # Add case insensitive flag
    case_insensitive = coerce_bool(kwargs.get("-i"), default=False)
    if case_insensitive:
        cmd_parts.append("--ignore-case")

    # Add context flags
    context_lines = coerce_int(kwargs.get("-C"))[0] if kwargs.get("-C") else None
    before_lines = coerce_int(kwargs.get("-B"))[0] if kwargs.get("-B") else None
    after_lines = coerce_int(kwargs.get("-A"))[0] if kwargs.get("-A") else None

    if context_lines:
        cmd_parts.append(f"--context={context_lines}")
    elif before_lines:
        cmd_parts.append(f"--before-context={before_lines}")
    elif after_lines:
        cmd_parts.append(f"--after-context={after_lines}")

    # Add glob pattern
    if glob:
        cmd_parts.append(f"--glob={glob}")

    # Add file type filter
    file_type = kwargs.get("type")
    if file_type:
        cmd_parts.append(f"--type={file_type}")

    # Add files-with-matches flag for count mode
    if output_mode == "files_with_matches":
        cmd_parts.append("--files-with-matches")
    elif output_mode == "count":
        cmd_parts.append("--count")

    # Add pattern - quote if it contains spaces
    if " " in pattern:
        cmd_parts.append(shlex.quote(pattern))
    else:
        cmd_parts.append(pattern)

    # Add path (default to current directory)
    search_path = path or "."
    cmd_parts.append(search_path)

    # Build command string
    command = " ".join(cmd_parts)

    # Check for duplicates
    is_duplicate, redirect_msg = check_for_duplicate(chat_manager, command)
    if is_duplicate:
        return redirect_msg

    # Execute command
    try:
        result = run_shell_command(
            command, repo_root, rg_exe_path, console, debug_mode, gitignore_spec
        )
        return result
    except Exception as e:
        return f"exit_code=1\nrg command failed: {str(e)}"
