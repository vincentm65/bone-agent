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
    user_intent: str = None,
) -> dict:
    """Run review sub-agent on a git diff.

    Args:
        diff_output: Git diff string to review
        repo_root: Repository root directory
        rg_exe_path: Path to rg executable
        console: Rich console for output
        chat_manager: ChatManager instance
        gitignore_spec: PathSpec for .gitignore filtering
        panel_updater: Optional SubAgentPanel for live updates
        user_intent: Optional description of what the user was trying to do

    Returns:
        Dict with keys:
            display: Clean review text (no injected file contents)
            history: Review text with file contents injected from citations
    """
    if not diff_output or not isinstance(diff_output, str) or not diff_output.strip():
        result = "No changes to review."
        return {"display": result, "history": result}

    # Build context (handles truncation and binary file notes)
    review_context, truncation_warning = _build_review_context(diff_output)

    if truncation_warning:
        console.print(f"[yellow]{truncation_warning}[/yellow]")

    # Set up panel updater
    if panel_updater is None:
        panel_updater = SimplePanelUpdater(console)

    # Task query for the review agent
    if user_intent:
        task_query = (
            "Analyze the diff provided above. "
            "For each changed file, use read_file to get surrounding context. "
            "Then provide a structured code review with findings, issues, and suggestions.\n\n"
            f"The user's intent for these changes was: {user_intent}\n"
            "Use this context to better understand the changes and focus your review accordingly."
        )
    else:
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
            result = f"exit_code=1\n{sub_agent_data['error']}"
            return {"display": result, "history": result}

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

        raw_result = sub_agent_data.get('result', '')

        # If hard limit was exceeded, skip injection and return raw dump for both
        if sub_agent_data.get('hard_limit_exceeded'):
            return {"display": raw_result, "history": raw_result}

        # If billed limit was exceeded, return partial results without injection
        if sub_agent_data.get('billed_limit_exceeded'):
            return {"display": raw_result, "history": raw_result}

        # Always inject file contents from citations into the history version
        injected_result = inject_file_contents(
            raw_result, repo_root, gitignore_spec, console
        )

        return {"display": raw_result, "history": injected_result}
