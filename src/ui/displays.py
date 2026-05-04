"""UI display functions for command outputs.""" 

from rich.table import Table
from rich.panel import Panel
from rich import box


def show_help_table(console):
    """Display command help table.

    Args:
        console: Rich Console instance for output.
    """
    console.print("")
    table = Table(show_header=True, box=box.SIMPLE_HEAD)
    table.add_column("Command", no_wrap=True)
    table.add_column("Description")

    table.add_row("[bold #5F9EA0]/help[/bold #5F9EA0]", "Show help")
    table.add_row("[bold #5F9EA0]/exit[/bold #5F9EA0]", "Exit chat")
    table.add_row("[bold #5F9EA0]/config[/bold #5F9EA0]", "Show all configuration settings")
    table.add_row("[bold #5F9EA0]/provider[/bold #5F9EA0] [name]", "Configure provider settings (model, key, costs)")
    table.add_row("[bold #5F9EA0]/key[/bold #5F9EA0] <key>", "Set API key for current provider")
    table.add_row("[bold #5F9EA0]/model[/bold #5F9EA0] <name>", "Set model for current provider")
    table.add_row("[bold #5F9EA0]/usage[/bold #5F9EA0] [provider] [in|out] <cost>", "Set/view provider-specific token cost")
    table.add_row("[bold #5F9EA0]/compact[/bold #5F9EA0]", "Compact context with an AI summary")


    table.add_row("[bold #5F9EA0]/cd[/bold #5F9EA0] [path]", "Change working directory (no args to show current)")
    table.add_row("[bold #5F9EA0]/edit[/bold #5F9EA0], [bold #5F9EA0]/e[/bold #5F9EA0]", "Open editor for multi-line input")
    table.add_row("[bold #5F9EA0]/review[/bold #5F9EA0] [args], [bold #5F9EA0]/r[/bold #5F9EA0]", "Code review git changes (e.g. /review --staged, /review main..HEAD)")
    table.add_row("[bold #5F9EA0]/ask[/bold #5F9EA0] [-c [N]] [-f] <query>, [bold #5F9EA0]/a[/bold #5F9EA0]", "Invoke sub-agent with a query (use -c N for context, -f for display only)")
    table.add_row("[bold #5F9EA0]/skills[/bold #5F9EA0] [list|add|modify|remove|use]", "Manage reusable prompt skills")
    table.add_row("[bold #5F9EA0]/obsidian[/bold #5F9EA0] [set|enable|disable|status|init]", "Manage vault integration, scaffold project folders")
    table.add_row("[bold #5F9EA0]/tools[/bold #5F9EA0] [list|enable|disable|enable-group|disable-group]", "Toggle tools or groups (e.g. file_ops, task_mgmt)")
    table.add_row("[bold #5F9EA0]/setup[/bold #5F9EA0]", "Re-run the first-run setup wizard")
    table.add_row("[bold #5F9EA0]/update[/bold #5F9EA0] [install]", "Check for or install npm package updates")
    table.add_row("[bold #5F9EA0]/cron[/bold #5F9EA0] [list|add|remove|enable|disable|run]", "Manage scheduled cron jobs")
    table.add_row("[bold #5F9EA0]/swarm[/bold #5F9EA0] <subcommand>", "Manage swarm pool (admin mode, worker spawn, task dispatch)")
    table.add_row("[bold #5F9EA0]:[/bold #5F9EA0]<command>", "Run a shell command (e.g. :git status)")


    console.print(Panel(table, title="[bold #5F9EA0]Commands[/bold #5F9EA0]", border_style="grey23", padding=(0, 2)))

    # Account management section
    console.print()
    acct_table = Table(show_header=True, box=box.SIMPLE_HEAD)
    acct_table.add_column("Command", no_wrap=True)
    acct_table.add_column("Description")

    acct_table.add_row("[bold #5F9EA0]/signup[/bold #5F9EA0] <email>", "Create bone-agent account and get API key")
    acct_table.add_row("[bold #5F9EA0]/login[/bold #5F9EA0]", "Log in to an existing bone-agent account")
    acct_table.add_row("[bold #5F9EA0]/account[/bold #5F9EA0]", "View account info and subscription status")
    acct_table.add_row("[bold #5F9EA0]/plan[/bold #5F9EA0]", "View available plans and pricing")
    acct_table.add_row("[bold #5F9EA0]/upgrade[/bold #5F9EA0]", "Upgrade or change your plan")
    acct_table.add_row("[bold #5F9EA0]/manage[/bold #5F9EA0]", "Cancel subscription or update payment (Stripe portal)")
    acct_table.add_row("[bold #5F9EA0]/rotate-key[/bold #5F9EA0]", "Invalidate current API key and generate a new one")
    acct_table.add_row("[bold #5F9EA0]/reset-key[/bold #5F9EA0]", "Get a new API key emailed to you (lost key recovery)")

    console.print(Panel(acct_table, title="[bold #5F9EA0]Account[/bold #5F9EA0]", border_style="grey23", padding=(0, 2)))

    # Keybinds section
    console.print()
    keybinds = Table(show_header=True, box=box.SIMPLE_HEAD)
    keybinds.add_column("Keybind", no_wrap=True)
    keybinds.add_column("Action")

    keybinds.add_row("Shift+Tab", "Cycle approval mode")
    keybinds.add_row("Ctrl+C", "Interrupt response")
    keybinds.add_row("Ctrl+C (2x)", "Exit program")

    console.print(Panel(keybinds, title="[bold #5F9EA0]Keybinds[/bold #5F9EA0]", border_style="grey23", padding=(0, 2)))
    console.print("")


def show_cron_help_table(console):
    """Display cron command help table.

    Args:
        console: Rich Console instance for output.
    """
    console.print("")
    table = Table(show_header=True, box=box.SIMPLE_HEAD)
    table.add_column("Command", no_wrap=True)
    table.add_column("Description")

    table.add_row("[bold #5F9EA0]/cron list[/bold #5F9EA0]", "Show all cron jobs (default)")
    table.add_row("[bold #5F9EA0]/cron add[/bold #5F9EA0] <id> <schedule> <cmd>", "Add a new cron job")
    table.add_row("[bold #5F9EA0]/cron remove[/bold #5F9EA0] <id>", "Remove a cron job")
    table.add_row("[bold #5F9EA0]/cron enable[/bold #5F9EA0] <id>", "Enable a cron job")
    table.add_row("[bold #5F9EA0]/cron disable[/bold #5F9EA0] <id>", "Disable a cron job")
    table.add_row("[bold #5F9EA0]/cron run[/bold #5F9EA0] <id>", "Run a job immediately (interactive)")
    table.add_row("[bold #5F9EA0]/cron allowlist[/bold #5F9EA0] [list|add|remove|clear]", "Manage allowed commands for a job")

    console.print(Panel(table, title="[bold #5F9EA0]Commands[/bold #5F9EA0]", border_style="grey23", padding=(0, 2)))

    # Schedule formats section
    console.print()
    sched_table = Table(show_header=True, box=box.SIMPLE_HEAD)
    sched_table.add_column("Format")
    sched_table.add_column("Example")

    sched_table.add_row("every <n> <unit>", "every 5 minutes, every 1 hour, every 3 days")
    sched_table.add_row("daily at <time>", "daily at 8am, daily at 17:30")
    sched_table.add_row("<day>s at <time>", "weekdays at 9am, mondays at 10:30pm")
    sched_table.add_row("<time>", "08:00, 17:30")

    console.print(Panel(sched_table, title="[bold #5F9EA0]Schedule Formats[/bold #5F9EA0]", border_style="grey23", padding=(0, 2)))
    console.print("")


def show_skills_help_table(console):
    """Display skills command help table.

    Args:
        console: Rich Console instance for output.
    """
    console.print("")
    table = Table(show_header=True, box=box.SIMPLE_HEAD)
    table.add_column("Command", no_wrap=True)
    table.add_column("Description")

    table.add_row("[bold #5F9EA0]/skills list[/bold #5F9EA0]", "List skills")
    table.add_row("[bold #5F9EA0]/skills add[/bold #5F9EA0] <name>", "Create a skill in your editor")
    table.add_row("[bold #5F9EA0]/skills edit[/bold #5F9EA0] <name>", "Edit an existing skill")
    table.add_row("[bold #5F9EA0]/skills modify[/bold #5F9EA0] <name> <prompt>", "Replace a skill")
    table.add_row("[bold #5F9EA0]/skills show[/bold #5F9EA0] <name>", "Show a skill")
    table.add_row("[bold #5F9EA0]/skills load[/bold #5F9EA0] <name>", "Load a skill into this chat")
    table.add_row("[bold #5F9EA0]/skills remove[/bold #5F9EA0] <name>", "Delete a skill")
    table.add_row("[bold #5F9EA0]/skills dir[/bold #5F9EA0]", "Show the skills directory")

    console.print(Panel(table, title="[bold #5F9EA0]Skills[/bold #5F9EA0]", border_style="grey23", padding=(0, 2)))
    console.print("")
