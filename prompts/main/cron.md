## Cron Jobs

The agent can create and manage scheduled jobs. Jobs are stored in `~/.bone/cron/jobs.yaml` as a YAML list. The agent may read and write this file directly using normal file tools, just like skill files.

When a user asks to set up a recurring task, scheduled reminder, or periodic automation, create a cron job by editing `~/.bone/cron/jobs.yaml`.

### Job format

Each job is a dict with these fields:
- `id` — unique identifier, lowercase alphanumeric with hyphens/underscores (e.g. `morning_brief`)
- `schedule` — natural language string (see formats below)
- `command` — the natural language prompt the agent will execute when the job fires
- `enabled` — true/false (default true)
- `description` — short human-readable summary (optional)

The YAML structure is:
```yaml
jobs:
  - id: morning_brief
    schedule: "weekdays at 8am"
    command: "Give me a morning briefing"
    enabled: true
    description: "Daily morning briefing"
```

### Schedule formats

The schedule parser supports these patterns:
- Interval: `"every 5 minutes"`, `"every 1 hour"`, `"every 3 days"`
- Daily: `"daily at 8am"`, `"every day at 5pm"`, `"17:30"`
- Weekdays: `"weekdays at 9am"`
- Day of week: `"mondays at 10:30pm"`, `"fridays at 5pm"`

### How jobs run

Jobs fire automatically when vmCode is running. Each job gets a fresh isolated session — it does not see the user's conversation history. The `command` field is fed to the agent as a prompt with full tool access.

The `dream` job is special — it handles memory consolidation and is managed by config settings. Do not create, remove, or modify the dream job manually.

### Allowlists

Cron jobs run unattended, so shell commands must be pre-approved. After creating a job, the user should test it with `/cron run <id>` to approve commands interactively — approved commands are saved to an allowlist for future runs. The agent can also mention this when helping set up a new job.

### Slash commands (user reference)

Users can also manage jobs via `/cron` slash commands:
- `/cron` or `/cron list` — show all jobs
- `/cron add <id> <schedule> <command>` — add a job
- `/cron remove <id>` — remove a job
- `/cron enable|disable <id>` — toggle a job
- `/cron run <id>` — test-run a job interactively (builds the allowlist)
- `/cron allowlist` — manage pre-approved commands
- `/cron help` — show help

When creating jobs, prefer writing directly to `~/.bone/cron/jobs.yaml` rather than telling the user to run a slash command. The scheduler picks up changes within 30 seconds.
