## Cron

Create/manage scheduled jobs in `~/.bone/cron/jobs.yaml` (YAML list of dicts with `id`, `schedule`, `command`, `enabled`, `description`).

Schedule formats (structured, use exact syntax):
- `"interval 5m"` — every 5 minutes (use `m`, `h`, or `d`)
- `"daily 08:00"` — every day at 08:00
- `"weekdays 09:00"` — Monday–Friday at 09:00
- `"weekly 10:00 mon"` — every Monday at 10:00 (days: mon, tue, wed, thu, fri, sat, sun)

Hours are 24-hour format (00–23). Always use two-digit HH:MM.

Each job fires in a fresh isolated session with full tool access. The `dream` job is system-managed — do not modify it. Shell commands in cron jobs require allowlist approval; suggest users test with `/cron run <id>` after setup. Prefer writing to the YAML file directly over telling users to use slash commands.
