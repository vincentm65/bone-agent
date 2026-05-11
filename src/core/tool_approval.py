"""Tool approval workflows for edit_file and execute_command."""

from rich.text import Text

from tools import confirm_tool


def handle_edit_approval(preview, file_path, args_dict, console, thinking_indicator,
                         approve_mode, cycle_approve_mode, repo_root, gitignore_spec,
                         vault_root_str, chat_manager=None):
    """Handle edit_file approval workflow.

    Args:
        preview: Either a rich Text object or a plain string to display.
        file_path: The file path being edited (for the confirm prompt).
        args_dict: Tool arguments dict (path, search, replace, context_lines).
        console: Rich console for display.
        thinking_indicator: ThinkingIndicator instance (may be None).
        approve_mode: Current approval mode string.
        cycle_approve_mode: Callable to cycle approval mode.
        repo_root: Repository root path string.
        gitignore_spec: Gitignore spec object.
        vault_root_str: Callable returning vault root path string.
        chat_manager: Optional ChatManager for admin interrupt polling.

    Returns:
        (result_str, should_exit) tuple where should_exit=True means cancel the agentic loop.
    """
    # Display preview
    console.print(preview)
    console.print()

    # Stop thinking indicator while waiting for user input
    if thinking_indicator:
        thinking_indicator.stop()

    action, guidance = confirm_tool(
        f"edit_file: {file_path}",
        console,
        reason=args_dict.get('reason', 'Apply file edit with above changes'),
        requires_approval=True,
        approve_mode=approve_mode,
        is_edit_tool=True,
        cycle_approve_mode=cycle_approve_mode,
        chat_manager=chat_manager,
    )

    if action == "accept":
        from tools.edit import _execute_edit_file
        final_result = _execute_edit_file(
            path=args_dict.get('path'),
            search=args_dict.get('search'),
            replace=args_dict.get('replace'),
            repo_root=repo_root,
            console=console,
            gitignore_spec=gitignore_spec,
            context_lines=args_dict.get('context_lines', 3),
            vault_root=vault_root_str()
        )
        # Strip exit_code line from final result before displaying
        if final_result and isinstance(final_result, str):
            result_lines = [line for line in final_result.split('\n') if not line.startswith('exit_code=')]
            final_result = '\n'.join(result_lines).strip()
        result_str, should_exit = final_result, False
    elif action == "advise":
        console.print(f"[dim]Edit not applied. User advice: {guidance}[/dim]")
        result_str = f"exit_code=1\nEdit not applied. User advice: {guidance}"
        should_exit = False
    else:  # cancel
        console.print("[dim]Operation canceled by user.[/dim]")
        result_str = "exit_code=2\nOperation canceled by user. Do not retry this operation."
        should_exit = True

    # Restart thinking indicator after user input
    if thinking_indicator:
        thinking_indicator.start()

    return result_str, should_exit


def resolve_edit_preview(result):
    """Extract a displayable preview from an edit_file tool result.

    Handles both Rich Text objects (new format) and legacy string format.

    Args:
        result: Either a rich Text object or a string.

    Returns:
        (preview, is_valid) tuple.
        - preview: Text object, plain string, or None if error.
        - is_valid: False if the result is an error (non-zero exit_code).
    """
    if isinstance(result, Text):
        return result, True
    elif isinstance(result, str) and result.startswith("exit_code=0"):
        lines = result.split('\n')
        preview_lines = [line for line in lines if not line.startswith("exit_code=")]
        preview = '\n'.join(preview_lines).strip()
        return preview, True
    else:
        # Error occurred during preview - don't show to user
        return None, False


def handle_command_approval(command, arguments, tool, context, console,
                            thinking_indicator, approve_mode, debug_mode,
                            cron_job_id=None, cron_allowlist=None,
                            cron_interactive=False, chat_manager=None,
                            **_kwargs):
    """Handle execute_command approval workflow.

    Checks for silent blocks, auto-approval, danger-mode non-git approval,
    and prompts user if needed. When a cron_job_id and cron_allowlist are
    provided, commands on the job's allow list are auto-approved; unlisted
    commands are blocked (in scheduled mode) or prompted interactively
    (in test-run mode).

    Args:
        command: The shell command string.
        arguments: Tool arguments dict (includes 'reason').
        tool: The tool object to execute on approval.
        context: Tool execution context dict.
        console: Rich console for display.
        thinking_indicator: ThinkingIndicator instance (may be None).
        approve_mode: Current approval mode string.
        debug_mode: Whether debug mode is active (for silent block logging).
        cron_job_id: Optional cron job ID for allow list checking.
        cron_allowlist: Optional CronAllowlist instance for cron command gating.
        cron_interactive: If True, cron job is in interactive test-run mode.
        chat_manager: Optional ChatManager for admin interrupt polling.

    Returns:
        (result, should_exit, command_executed) tuple.
        - result: Tool result string.
        - should_exit: True if the user canceled (break the agentic loop).
        - command_executed: True if the command was actually executed (display output).
    """
    from utils.safe_commands import is_git_command
    from utils.validation import is_auto_approved_command, check_for_silent_blocked_command

    # Check if command should be silently blocked (redirect to native tool)
    is_blocked, reprompt_msg = check_for_silent_blocked_command(command)
    if is_blocked:
        if debug_mode:
            console.print(f"[dim]Silently blocked command: {command.split()[0]}[/dim]")
        result = f"exit_code=1\n{reprompt_msg}"
        return result, False, False

    # Check if command should be auto-approved (global safe commands)
    auto_approve = is_auto_approved_command(command)

    # Check cron allow list
    cron_auto_approved = False
    if cron_job_id and cron_allowlist:
        if cron_allowlist.is_allowed(cron_job_id, command):
            cron_auto_approved = True
        elif not auto_approve:
            # Command not on allow list and not globally safe
            # Determine if we're in interactive test-run or scheduled mode
            if cron_interactive:
                # Interactive test run (/cron run) — prompt the user
                pass  # Fall through to normal interactive approval below
            else:
                # Scheduled run — block the command, let agent adapt
                allowed_cmds = cron_allowlist.get_commands(cron_job_id)
                allowed_preview = ", ".join(f"'{c}'" for c in allowed_cmds[:5])
                if len(allowed_cmds) > 5:
                    allowed_preview += f", ... ({len(allowed_cmds)} total)"
                if not allowed_preview:
                    allowed_preview = "(none - run '/cron run <id>' to build the allow list)"
                result = (
                    f"exit_code=1\n"
                    f"Command not in cron allow list for job '{cron_job_id}'.\n"
                    f"Command: {command}\n"
                    f"Allowed: {allowed_preview}\n"
                    f"Do not retry this command. Use only approved commands or "
                    f"ask the user to run '/cron run {cron_job_id}' to add it."
                )
                return result, False, False

    danger_auto_approved = approve_mode == "danger" and not is_git_command(command)

    if cron_auto_approved or auto_approve or danger_auto_approved:
        # Auto-approved command - execute without prompting
        result = tool.execute(arguments, context)
        command_executed = True

        # In cron test-run mode, auto-save newly approved commands to allow list
        # Skip globally-safe commands and danger-mode approvals — they do not need
        # per-job allow list entries.
        if cron_job_id and cron_allowlist and cron_interactive and not auto_approve and not danger_auto_approved:
            cron_allowlist.add_command(cron_job_id, command)

        return result, False, command_executed

    # Interactive approval (test-run mode or normal session)
    # Stop thinking indicator while waiting for user input
    if thinking_indicator:
        thinking_indicator.stop()

    action, guidance = confirm_tool(
        f"execute_command: {command[:80]}{'...' if len(command) > 80 else ''}",
        console,
        reason=arguments.get('reason', 'Execute shell command'),
        requires_approval=True,
        approve_mode=approve_mode,
        chat_manager=chat_manager,
    )

    if action == "accept":
        result = tool.execute(arguments, context)
        command_executed = True
        # Auto-save approved command to cron allow list during test run
        if cron_job_id and cron_allowlist:
            cron_allowlist.add_command(cron_job_id, command)
    elif action == "advise":
        result = f"exit_code=1\nCommand not executed. User advice: {guidance}"
        command_executed = False
    elif action == "cancel":
        result = "exit_code=2\nCommand canceled by user. Do not retry this operation."
        if thinking_indicator:
            thinking_indicator.start()
        return result, True, False

    # Restart thinking indicator after user input
    if thinking_indicator:
        thinking_indicator.start()

    return result, False, command_executed
