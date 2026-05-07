"""Sub-agent tool for complex multi-file exploration."""

from pathlib import Path

from .helpers.base import tool
from core.sub_agent import run_sub_agent
from utils.citation_parser import inject_file_contents


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

    def cancel(self):
        """No-op cancellation for simple panel (no live display to clear)."""
        pass


@tool(
    name="sub_agent",
    description="Required: Call this first before any rg or read_file when answering 'how something works', architecture, patterns, multi-file flows, or broad exploration. Do not search manually — this tool is 10x faster. Examples: 'How does authentication work?', 'Explain the data flow', 'Where is X handled?'",
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
    requires_approval=False,
    terminal_policy="yield"
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

    # Clear any stale cancellation flag before starting a new sub-agent
    chat_manager.clear_subagent_cancel()

    # Use panel for streaming tool output
    with panel_updater as panel:
        sub_agent_data = run_sub_agent(
            task_query=query,
            repo_root=repo_root,
            rg_exe_path=rg_exe_path,
            console=console,
            panel_updater=panel,
            cancel_event=chat_manager.get_subagent_cancel_event(),
        )

        # Check for cancellation first (before error, to avoid error display)
        if sub_agent_data.get('cancelled'):
            usage = sub_agent_data.get('usage', {})
            if usage:
                chat_manager.token_tracker.add_usage(usage, model_name=sub_agent_data.get("model", ""))
            panel.cancel()
            return "exit_code=130\nSubagent cancelled by user."

        # Check for preflight context overflow (initial context exceeds hard limit)
        if sub_agent_data.get('preflight_overflow'):
            tokens = sub_agent_data.get('preflight_tokens', 0)
            limit = sub_agent_data.get('hard_limit', 0)
            panel.cancel()
            msg = (
                f"Subagent cannot start: initial context ({tokens:,} tokens) "
                f"exceeds hard limit ({limit:,} tokens). "
                "Try compacting the main session (/compact), clearing context (/clear), "
                "or reducing the amount of injected context."
            )
            console.print(f"[dim red]╰─ preflight overflow: {tokens:,} / {limit:,} tokens[/dim red]", highlight=False)
            console.file.flush()
            return f"exit_code=1\n{msg}"

        # Check for errors
        if sub_agent_data.get('error'):
            panel.set_error(sub_agent_data['error'])
            error_summary = f"[red]✗ subagent error[/red]"
            console.print(error_summary, highlight=False)
            console.file.flush()
            return f"exit_code=1\n{sub_agent_data['error']}"

        # Track usage
        usage = sub_agent_data.get('usage', {})
        if usage:
            chat_manager.token_tracker.add_usage(usage, model_name=sub_agent_data.get("model", ""))
            panel.set_complete({
                'prompt_tokens': usage.get('prompt_tokens', 0),
                'completion_tokens': usage.get('completion_tokens', 0),
                'total_tokens': usage.get('total_tokens', 0),
                'context_tokens': usage.get('context_tokens', 0),
            })

            # Print completion summary in chat
            total_tools = panel.total_tool_calls
            total_tok = usage.get('total_tokens', 0)
            summary_parts = [f"{total_tools} tools"]
            if total_tok:
                summary_parts.append(f"{total_tok:,} tokens")
            summary_line = f"[green]✓[/green] subagent done: [dim]{' | '.join(summary_parts)}[/dim]"
            console.print(summary_line, highlight=False)
            console.file.flush()

        # Display sub-agent result summary (used for context)
        raw_result = sub_agent_data.get('result', '')

        # If hard limit was exceeded, clear toolbar and return context dump
        if sub_agent_data.get('hard_limit_exceeded'):
            panel.cancel()
            tokens = sub_agent_data.get('context_tokens', 0)
            limit = sub_agent_data.get('hard_limit_tokens', 0)
            console.print(
                f"[dim red]╰─ hard limit overflow: {tokens:,} / {limit:,} tokens[/dim red]",
                highlight=False,
            )
            console.file.flush()
            return raw_result

        # If billed limit was exceeded, show warning and return partial results
        if sub_agent_data.get('billed_limit_exceeded'):
            panel.cancel()
            billed_total = sub_agent_data.get('billed_total_tokens', 0)
            billed_limit = sub_agent_data.get('billed_hard_limit_tokens', 0)
            console.print(
                f"[dim yellow]╰─ token budget exhausted: {billed_total:,} / {billed_limit:,} tokens burned[/dim yellow]",
                highlight=False,
            )
            console.file.flush()
            # Skip file injection — partial results should go back as-is
            return raw_result

        # Parse and inject file contents
        injected_result = inject_file_contents(
            raw_result, repo_root, gitignore_spec, console
        )

        return injected_result


