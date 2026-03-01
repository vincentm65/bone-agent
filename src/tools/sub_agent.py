"""Sub-agent tool for complex multi-file exploration."""

import re
from pathlib import Path
from typing import Optional

from .helpers.base import tool
from core.sub_agent import run_sub_agent


class SimplePanelUpdater:
    """Simple panel updater for non-parallel tool execution.

    This is a fallback implementation used when panel_updater is None,
    typically in sequential mode where live updates aren't needed.
    """

    def __init__(self, console):
        """Initialize the simple panel updater.

        Args:
            console: Rich console for output
        """
        self.console = console
        self.total_tool_calls = 0

    def __enter__(self):
        """Enter context manager."""
        return self

    def __exit__(self, *args):
        """Exit context manager."""
        pass

    def append(self, text):
        """Append text to panel (no-op in simple mode)."""
        pass  # No live updates in sequential mode

    def add_tool_call(self, tool_name, tool_result=None, command=None):
        """Track a tool call."""
        self.total_tool_calls += 1

    def set_complete(self, usage=None):
        """Mark panel as complete."""
        pass

    def set_error(self, message):
        """Display error message."""
        self.console.print(f"[red]Sub-Agent Error: {message}[/red]")


@tool(
    name="sub_agent",
    description="MANDATORY: MUST CALL THIS FIRST before ANY rg or read_file when answering: 'how something works', architecture, patterns, multi-file flows, or broad exploration. DO NOT search manually - this tool is 10x faster. Examples: 'How does authentication work?', 'Explain the data flow', 'Where is X handled?'",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Task query, e.g. 'How does the chat manager handle history?'"
            }
        },
        "required": ["query"]
    },
    allowed_modes=["edit", "plan", "learn"],
    requires_approval=False
)
def sub_agent(
    query: str,
    repo_root: Path,
    rg_exe_path: str,
    console,
    chat_manager,
    gitignore_spec = None,
    panel_updater = None
) -> str:
    """Run sub-agent for complex multi-file exploration.

    Args:
        query: Task query for the sub-agent
        repo_root: Repository root directory (injected by context)
        rg_exe_path: Path to rg executable (injected by context)
        console: Rich console for output (injected by context)
        chat_manager: ChatManager instance (injected by context)
        gitignore_spec: PathSpec for .gitignore filtering (injected by context)
        panel_updater: Optional SubAgentPanel for live updates (injected by context)

    Returns:
        Sub-agent result with injected file contents
    """
    if not query or not isinstance(query, str) or not query.strip():
        return "exit_code=1\nsub_agent requires a non-empty 'query' argument."

    # Import SimplePanelUpdater if not provided
    if panel_updater is None:
        # If running in sequential mode, create a simple panel updater
        panel_updater = SimplePanelUpdater(console)

    # Use panel for streaming tool output
    with panel_updater as panel:
        sub_agent_data = run_sub_agent(
            task_query=query,
            repo_root=repo_root,
            rg_exe_path=rg_exe_path,
            console=console,
            panel_updater=panel,
        )

        # Check for errors
        if sub_agent_data.get('error'):
            panel.set_error(sub_agent_data['error'])
            return f"exit_code=1\n{sub_agent_data['error']}"

        # Track usage
        usage = sub_agent_data.get('usage', {})
        if usage:
            chat_manager.token_tracker.add_usage(usage)
            panel.set_complete({
                'prompt_tokens': usage.get('prompt_tokens', 0),
                'completion_tokens': usage.get('completion_tokens', 0),
                'total_tokens': usage.get('total_tokens', 0)
            })

        # Display sub-agent result summary (used for context)
        raw_result = sub_agent_data.get('result', '')

        # Parse and inject file contents
        injected_files_content = _parse_and_inject_files(
            raw_result, repo_root, gitignore_spec, console
        )

        # Combine raw result with injected content
        if injected_files_content:
            final_result = raw_result + "\n\n## Injected File Contents\n\n" + "\n".join(injected_files_content)
        else:
            final_result = raw_result

        return final_result


def _calculate_line_range(start_line: int, end_line: int) -> int:
    """Calculate max_lines from start and end line numbers.

    Args:
        start_line: Starting line number (1-based, inclusive)
        end_line: Ending line number (1-based, inclusive)

    Returns:
        Number of lines in the range
    """
    return end_line - start_line + 1


def _parse_and_inject_files(raw_result, repo_root, gitignore_spec, console):
    """Parse sub-agent result and inject file contents.

    Args:
        raw_result: Sub-agent result text
        repo_root: Repository root directory
        gitignore_spec: PathSpec for .gitignore filtering
        console: Rich console for output

    Returns:
        List of injected file content strings
    """
    injected_files_content = []

    # Regex to find explicit citation patterns (bracketed notation only for safety)
    file_pattern = re.compile(
        r"(?:-\s+\[(.*?)\]\s+\((?:lines\s+)?(\d+)-(\d+)(?:\s*lines)?|full)\)|"
        r"(?:lines\s+(\d+)-(\d+)\s+in\s+\[(.*?)\])|"
        r"(?:\[(.*?)\]:(\d+)-(\d+))|"
        r"(?:\[(.*?)\]:(\d+))|"
        r"(?:\[(.*?)\](?![:(]))"
    )

    for line in raw_result.split('\n'):
        match = file_pattern.search(line)
        if match:
            # Extract path and range from whichever pattern matched
            if match.group(1):
                # Pattern 1: - [file] (N-M) or (full)
                rel_path = match.group(1).strip()
                if match.group(2) and match.group(3):
                    start_line = int(match.group(2))
                    end_line = int(match.group(3))
                    max_lines = _calculate_line_range(start_line, end_line)
                else:
                    start_line = 1
                    max_lines = None
            elif match.group(4) and match.group(5) and match.group(6):
                # Pattern 2: lines N-M in [file]
                start_line = int(match.group(4))
                end_line = int(match.group(5))
                rel_path = match.group(6).strip()
                max_lines = _calculate_line_range(start_line, end_line)
            elif match.group(7) and match.group(8) and match.group(9):
                # Pattern 3: [file]:N-M
                rel_path = match.group(7).strip()
                start_line = int(match.group(8))
                end_line = int(match.group(9))
                max_lines = _calculate_line_range(start_line, end_line)
            elif match.group(10) and match.group(11):
                # Pattern 4: [file]:N (single line)
                rel_path = match.group(10).strip()
                start_line = int(match.group(11))
                max_lines = 1
            elif match.group(12):
                # Pattern 5: [file] (full)
                rel_path = match.group(12).strip()
                start_line = 1
                max_lines = None
            else:
                continue

            try:
                from .file_reader import read_file as read_file_with_bypass

                tool_result = read_file_with_bypass(
                    rel_path,
                    repo_root,
                    max_lines=max_lines,
                    start_line=start_line,
                    gitignore_spec=gitignore_spec,
                )

                # Check for exit code
                first_line = tool_result.split('\n')[0] if tool_result else ""
                if first_line.startswith("exit_code="):
                    exit_code = first_line.split("=")[1].split()[0]
                    if exit_code != "0":
                        injected_files_content.append(f"### {rel_path} (Blocked or unavailable)")
                        injected_files_content.append(tool_result.strip())
                        injected_files_content.append("")
                        continue

                # Add to injected content (strip metadata line)
                content_lines = tool_result.splitlines()[1:] if isinstance(tool_result, str) else []
                content = "\n".join(content_lines).rstrip()

                # Parse actual lines_read and start_line from metadata
                lines_read_match = re.search(r'lines_read=(\d+)', first_line)
                start_line_match = re.search(r'start_line=(\d+)', first_line)

                if lines_read_match:
                    actual_lines_read = int(lines_read_match.group(1))
                    actual_start_line = int(start_line_match.group(1)) if start_line_match else start_line

                    if actual_start_line > 1:
                        end_line = actual_start_line + actual_lines_read - 1
                        header_info = f"lines {actual_start_line}-{end_line} ({actual_lines_read} line{'s' if actual_lines_read != 1 else ''})"
                    else:
                        header_info = f"lines {actual_start_line}-{actual_lines_read} ({actual_lines_read} line{'s' if actual_lines_read != 1 else ''})"
                else:
                    header_info = "full"

                injected_files_content.append(f"### {rel_path} ({header_info})")
                injected_files_content.append("```")
                injected_files_content.append(content)
                injected_files_content.append("```\n")

            except Exception as e:
                injected_files_content.append(f"### {rel_path} (Error reading file: {e})")

    return injected_files_content
