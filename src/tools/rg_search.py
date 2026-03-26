"""Ripgrep search tool."""

import shlex
from pathlib import Path
from typing import Optional

from .helpers.base import tool
from .shell import run_shell_command
from .helpers.converters import coerce_bool, coerce_int


@tool(
    name="rg",
    description="Search files using ripgrep. Use for ALL code searches (never use shell commands). Supports regex, file filtering (glob/type), and multiple output modes: content (matches with context), files_with_matches (paths), or count.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regular expression pattern to search for"
            },
            "path": {
                "type": "string",
                "description": "File or directory to search (default: current directory)"
            },
            "glob": {
                "type": "string",
                "description": "Glob pattern to filter files (e.g. \"*.js\", \"**/*.tsx\")"
            },
            "type": {
                "type": "string",
                "description": "File type to search (e.g. js, py, rust, go, java)"
            },
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_with_matches", "count"],
                "description": "Output mode (default: files_with_matches)"
            },
            "context_lines": {
                "type": "integer",
                "description": "Context lines before/after matches (requires output_mode: content)"
            },
            "case_insensitive": {
                "type": "boolean",
                "description": "Case insensitive search"
            },
            "multiline": {
                "type": "boolean",
                "description": "Enable multiline mode (patterns can span lines)"
            }
        },
        "required": ["pattern"]
    },
    allowed_modes=["edit", "plan"],
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
        **kwargs: Additional keyword arguments (type, multiline, context_lines, case_insensitive)

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
    case_insensitive = coerce_bool(kwargs.get("case_insensitive"), default=False)
    if case_insensitive:
        cmd_parts.append("--ignore-case")

    # Add context lines flag
    context_lines = coerce_int(kwargs.get("context_lines"))[0] if kwargs.get("context_lines") else None
    if context_lines:
        cmd_parts.append(f"--context={context_lines}")

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

    # Execute command
    try:
        result = run_shell_command(
            command, repo_root, rg_exe_path, console, debug_mode, gitignore_spec
        )
        return result
    except Exception as e:
        return f"exit_code=1\nrg command failed: {str(e)}"
