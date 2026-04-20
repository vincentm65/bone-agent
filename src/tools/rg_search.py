"""Ripgrep search tool."""

import logging
import re
import shlex
import subprocess
from pathlib import Path
from typing import Optional

from .helpers.base import tool
from .helpers.formatters import format_tool_result
from .shell import _prepare_execution_environment, run_shell_command
from .helpers.converters import coerce_bool, coerce_int

logger = logging.getLogger(__name__)

# Default match limit for vault searches (separate from repo limit)
_VAULT_MAX_MATCHES = 20


@tool(
    name="rg",
    description="Search files using ripgrep. Use for ALL code searches (never shell commands). Supports regex, glob/type filtering, and output modes: content, files_with_matches, or count.",
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
                "description": "Glob filter (e.g. \"*.js\", \"**/*.tsx\")"
            },
            "type": {
                "type": "string",
                "description": "File type (e.g. js, py, rust, go, java)"
            },
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_with_matches", "count"],
                "description": "Output mode (default: files_with_matches)"
            },
            "context_lines": {
                "type": "integer",
                "description": "Context lines around matches (requires output_mode: content)"
            },
            "case_insensitive": {
                "type": "boolean",
                "description": "Case insensitive search"
            },
            "multiline": {
                "type": "boolean",
                "description": "Patterns can span lines"
            },
            "max_matches": {
                "type": "integer",
                "description": "Max matches across all files (default: 100, 0 = use line limit)"
            }
        },
        "required": ["pattern"]
    },
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
    vault_root: Optional[str] = None,
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
        vault_root: Obsidian vault root path (injected by context)
        **kwargs: Additional keyword arguments (type, multiline, context_lines, case_insensitive)

    Returns:
        Search results with exit code. Vault results are included in a
        separate section when vault_root is active.
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

    # Get max_matches from kwargs (default: 100, set to 0 for no limit)
    raw = coerce_int(kwargs.get("max_matches"))[0] if kwargs.get("max_matches") is not None else None
    max_matches = raw if raw is not None and raw >= 0 else 100

    # Execute repo search
    try:
        repo_result = run_shell_command(
            command, repo_root, rg_exe_path, console, debug_mode, gitignore_spec,
            max_matches=max_matches
        )
    except Exception as e:
        return f"exit_code=1\nrg command failed: {str(e)}"

    # If no vault configured, return repo results directly
    if not vault_root:
        return repo_result

    # Run vault search and merge results
    vault_output = _search_vault(
        vault_root, rg_exe_path, output_mode, debug_mode, console,
        pattern=pattern,
        glob=glob,
        file_type=kwargs.get("type"),
        case_insensitive=case_insensitive,
        multiline=multiline,
        context_lines=context_lines,
        max_matches=_VAULT_MAX_MATCHES,
    )

    if not vault_output:
        return repo_result

    # Merge results: repo section + vault section with absolute paths
    return _merge_results(repo_result, vault_output, output_mode)


def _search_vault(vault_root, rg_exe_path, output_mode, debug_mode, console,
                   pattern, glob=None, file_type=None, case_insensitive=False,
                   multiline=False, context_lines=None, max_matches=20):
    """Run rg against the vault and return formatted output string (or None).

    Builds its own command from explicit parameters to avoid mutating any
    shared state. Uses direct subprocess with cwd=vault_root so that
    any .gitignore in the vault doesn't filter searchable content.
    """
    vault_path = Path(vault_root).resolve()
    if not vault_path.is_dir():
        return None

    try:
        # Build exclude globs from obsidian_settings
        from utils.settings import obsidian_settings
        try:
            exclude_folders = obsidian_settings.exclude_folders_list
        except (AttributeError, TypeError):
            exclude_folders = []

        # Build vault rg command from scratch (no shared state mutation)
        vault_args = ["--no-ignore"]

        # Add exclude folders first so they can't be overridden
        for folder in exclude_folders:
            vault_args.append(f"--glob=!{folder}")

        # Add output-mode flags
        if output_mode == "content":
            vault_args.append("--line-number")
        elif output_mode == "files_with_matches":
            vault_args.append("--files-with-matches")
        elif output_mode == "count":
            vault_args.append("--count")

        # Add search flags
        if multiline:
            vault_args.append("-U")
            vault_args.append("--multiline-dotall")
        if case_insensitive:
            vault_args.append("--ignore-case")
        if context_lines:
            vault_args.append(f"--context={context_lines}")
        if glob:
            vault_args.append(f"--glob={glob}")
        if file_type:
            vault_args.append(f"--type={file_type}")

        # Pattern and search path (always "." since cwd is vault root)
        # No quoting needed — subprocess list args bypass the shell
        vault_args.append(pattern)
        vault_args.append(".")

        # Prepare environment with rg on PATH
        env = _prepare_execution_environment(vault_path, rg_exe_path)

        if debug_mode and console:
            console.print(f"[dim]→ Vault search: rg {' '.join(vault_args)}[/dim]")
            console.print(f"[dim]→ Vault cwd: {vault_path}[/dim]")

        result = subprocess.run(
            [str(rg_exe_path)] + vault_args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            cwd=str(vault_path),
            env=env,
        )

        if debug_mode and console:
            console.print(f"[dim]→ Vault exit code: {result.returncode}[/dim]")

        # rg returns 1 for no matches — that's fine, just no vault results
        if result.returncode == 2:
            logger.debug("Vault rg error (exit 2): %s", result.stderr.strip())
            return None

        output = (result.stdout or "").strip()
        if not output:
            return None

        # Format vault output with its own match limit
        formatted = format_tool_result(
            result,
            command="rg " + " ".join(vault_args),
            is_rg=True,
            debug_mode=debug_mode,
            max_matches=max_matches,
        )

        # Prefix vault paths with absolute vault root for clarity.
        # Only rewrite lines that look like they start with an rg file path.
        # rg output: "relative/path:linenum:match" or "relative/path-linenum-context"
        # or "relative/path:count" (count mode).  Must contain / or . before any
        # colon to avoid matching content-only lines or binary headers.
        _path_line_re = re.compile(r"^[^\s:|].*[/.]")
        vault_prefix = str(vault_path)

        lines = formatted.split("\n")
        rewritten = []
        for line in lines:
            # Skip metadata lines (exit_code, matches/files)
            if line.startswith("exit_code=") or line.startswith("matches=") or line.startswith("files="):
                rewritten.append(line)
                continue
            if not line.strip() or line.startswith("... ("):
                rewritten.append(line)
                continue
            # Only rewrite lines that start with a relative path
            m = _path_line_re.match(line)
            if m:
                rewritten.append(f"{vault_prefix}/{line}")
            else:
                rewritten.append(line)

        return "\n".join(rewritten)

    except Exception:
        logger.warning("Vault search failed", exc_info=True)
        return None


def _merge_results(repo_result, vault_output, output_mode):
    """Merge repo and vault results into a single response.

    Both inputs are raw formatted strings from format_tool_result.
    We extract the content sections and combine them under headers.
    Metadata (matches/files counts) is preserved so the display parser
    can extract a summary for the user.
    """
    def _extract_content(formatted):
        """Extract content lines (skip metadata header)."""
        lines = formatted.split("\n")
        content_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("exit_code=") or stripped.startswith("matches=") or stripped.startswith("files="):
                continue
            if stripped.startswith("... ("):
                continue
            content_lines.append(line)
        return "\n".join(content_lines).strip()

    def _extract_exit_code(formatted):
        for line in formatted.split("\n"):
            if line.startswith("exit_code="):
                return line.split("=", 1)[1]
        return "0"

    def _extract_count(formatted):
        """Extract matches=N or files=N count from formatted result."""
        for line in formatted.split("\n"):
            if line.startswith("matches="):
                try:
                    return ("matches", int(line.split("=", 1)[1].strip()))
                except (ValueError, IndexError):
                    pass
            elif line.startswith("files="):
                try:
                    return ("files", int(line.split("=", 1)[1].strip()))
                except (ValueError, IndexError):
                    pass
        return None

    repo_exit_code = _extract_exit_code(repo_result)
    repo_content = _extract_content(repo_result)
    vault_content = _extract_content(vault_output)

    if not vault_content:
        return repo_result

    # Build combined metadata line for the display parser
    repo_count = _extract_count(repo_result)
    vault_count = _extract_count(vault_output)

    metadata_line = ""
    if repo_count and vault_count and repo_count[0] == vault_count[0]:
        # Same count type (both matches or both files) — sum them
        combined = repo_count[1] + vault_count[1]
        metadata_line = f"{repo_count[0]}={combined}"
    elif repo_count:
        metadata_line = f"{repo_count[0]}={repo_count[1]}"
    elif vault_count:
        metadata_line = f"{vault_count[0]}={vault_count[1]}"

    if not repo_content:
        # Only vault results — return with vault label
        header = f"exit_code=0"
        if metadata_line:
            header += f"\n{metadata_line}"
        return f"{header}\n[vault]\n{vault_content}\n\n"

    # Both — present under labeled sections, preserve repo exit code
    header = f"exit_code={repo_exit_code}"
    if metadata_line:
        header += f"\n{metadata_line}"
    merged = f"{header}\n[repo]\n{repo_content}\n\n[vault]\n{vault_content}\n\n"
    return merged
