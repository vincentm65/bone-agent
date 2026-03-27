"""Review sub-agent tool for analyzing git diffs."""

import re
from pathlib import Path

from core.sub_agent import run_sub_agent
from tools.sub_agent import SimplePanelUpdater
from utils.citation_parser import inject_file_contents

# Approximate chars per token (conservative heuristic, avoids tokenizer dependency)
_CHARS_PER_TOKEN = 4

# Max tokens for the diff context (leaves room for agent conversation within budget)
_MAX_DIFF_TOKENS = 50_000


def _estimate_tokens(text: str) -> int:
    """Rough token estimate based on character count."""
    return len(text) // _CHARS_PER_TOKEN


def _truncate_diff(diff: str, max_chars: int) -> tuple:
    """Truncate a diff to fit within a character budget, keeping whole files.

    Returns:
        Tuple of (truncated_diff, list_of_skipped_file_names)
    """
    # Split on diff headers: each file starts with "diff --git"
    file_blocks = re.split(r'(?=^diff --git )', diff, flags=re.MULTILINE)
    # First element may be empty if diff starts with "diff --git"
    if file_blocks and file_blocks[0].strip() == '':
        file_blocks = file_blocks[1:]

    result_blocks = []
    skipped_files = []
    current_chars = 0

    for block in file_blocks:
        block_chars = len(block)
        if current_chars + block_chars > max_chars:
            # Extract file name from the block header for the skip list
            match = re.search(r'^diff --git a/(.*?) b/', block, re.MULTILINE)
            if match:
                skipped_files.append(match.group(1))
        else:
            result_blocks.append(block)
            current_chars += block_chars

    truncated = '\n'.join(result_blocks)
    return truncated, skipped_files


def _count_binary_files(diff: str) -> int:
    """Count binary file entries in a diff."""
    return len(re.findall(r'^Binary files ', diff, re.MULTILINE))


def _build_review_context(diff: str) -> str:
    """Build the initial context message for the review sub-agent.

    Handles truncation and binary file counting.

    Returns:
        Tuple of (context_string, warning_string_or_None)
    """
    warning = None
    max_chars = _MAX_DIFF_TOKENS * _CHARS_PER_TOKEN

    # Check for binary files
    binary_count = _count_binary_files(diff)
    binary_note = ""
    if binary_count > 0:
        binary_note = f"\n\nNote: {binary_count} binary file(s) were skipped in this diff."

    # Check if truncation is needed
    estimated_tokens = _estimate_tokens(diff)
    if estimated_tokens > _MAX_DIFF_TOKENS:
        truncated_diff, skipped_files = _truncate_diff(diff, max_chars)
        skipped_note = ""
        if skipped_files:
            skipped_note = (
                f"\n\nWARNING: Diff was too large and has been truncated. "
                f"The following {len(skipped_files)} file(s) were omitted from review:\n"
                + "\n".join(f"  - {f}" for f in skipped_files)
            )
        warning = f"Diff truncated: {len(skipped_files)} file(s) omitted."
        context = f"Review this git diff:\n\n{truncated_diff}{binary_note}{skipped_note}"
    else:
        context = f"Review this git diff:\n\n{diff}{binary_note}"

    return context, warning


def review_changes(
    diff_output: str,
    repo_root: Path,
    rg_exe_path: str,
    console,
    chat_manager,
    gitignore_spec=None,
    panel_updater=None,
    skip_citation_injection=False,
) -> str:
    """Run review sub-agent on a git diff.

    Args:
        diff_output: Git diff string to review
        repo_root: Repository root directory
        rg_exe_path: Path to rg executable
        console: Rich console for output
        chat_manager: ChatManager instance
        gitignore_spec: PathSpec for .gitignore filtering
        panel_updater: Optional SubAgentPanel for live updates
        skip_citation_injection: If True, return raw result without
            injecting file contents (useful when output goes to user)

    Returns:
        Review result (with injected file contents unless skip_citation_injection)
    """
    if not diff_output or not isinstance(diff_output, str) or not diff_output.strip():
        return "exit_code=0\nNo changes to review."

    # Build context (handles truncation and binary file notes)
    review_context, truncation_warning = _build_review_context(diff_output)

    if truncation_warning:
        console.print(f"[yellow]{truncation_warning}[/yellow]")

    # Set up panel updater
    if panel_updater is None:
        panel_updater = SimplePanelUpdater(console)

    # Task query for the review agent
    task_query = (
        "Analyze the diff provided above. "
        "For each changed file, use read_file to get surrounding context. "
        "Then provide a structured code review with findings, issues, and suggestions."
    )

    with panel_updater as panel:
        sub_agent_data = run_sub_agent(
            task_query=task_query,
            repo_root=repo_root,
            rg_exe_path=rg_exe_path,
            console=console,
            panel_updater=panel,
            sub_agent_type="review",
            initial_context=review_context,
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

        raw_result = sub_agent_data.get('result', '')

        # Parse and inject file contents from citations
        # (skip when output goes directly to user to avoid false positives
        # from bracket syntax like [critical], [warning], etc.)
        if skip_citation_injection:
            return raw_result

        injected_result = inject_file_contents(
            raw_result, repo_root, gitignore_spec, console
        )

        return injected_result
