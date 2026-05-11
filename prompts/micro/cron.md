## Cron

Create/manage scheduled jobs in `~/.bone/cron/jobs.yaml` (YAML list of dicts with `id`, `schedule`, `command`, `enabled`, `description`).

Schedule formats: interval (`"every 5 minutes"`), daily (`"daily at 8am"`), weekdays (`"weekdays at 9am"`), day-of-week (`"mondays at 10pm"`).

Each job fires in a fresh isolated session with full tool access. The `dream` job is system-managed — do not modify it. Shell commands in cron jobs require allowlist approval; suggest users test with `/cron run <id>` after setup. Prefer writing to the YAML file directly over telling users to use slash commands.
