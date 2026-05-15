"""Per-job command allow list for cron jobs.

Stores approved shell commands per job ID in ~/.bone/cron/allowed_commands.yaml.
During scheduled runs, only commands on the allow list (plus global SAFE_COMMAND_RULES)
are auto-approved. Unlisted commands are blocked with agent feedback.

During interactive test runs (/cron run), accepted commands are auto-saved.
"""

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def _get_allowed_commands_path() -> Path:
    """Return ~/.bone/cron/allowed_commands.yaml."""
    cron_dir = Path.home() / ".bone" / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    return cron_dir / "allowed_commands.yaml"


class CronAllowlist:
    """Manages per-job shell command allow lists.

    Storage format (YAML):
        jobs:
          my_job:
            commands:
              - "git add -A"
              - "git commit -m 'auto commit'"
              - "git push origin main"
    """

    def __init__(self):
        self._path = _get_allowed_commands_path()
        self._jobs: dict[str, list[str]] = {}
        self.load()

    def load(self):
        """Load allow list from disk."""
        self._jobs.clear()
        if not self._path.exists():
            return
        try:
            data = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
            for job_id, entry in data.get("jobs", {}).items():
                self._jobs[job_id] = entry.get("commands", [])
        except Exception as e:
            logger.warning("Failed to load cron allow list: %s", e)

    def save(self):
        """Persist allow list to disk."""
        data = {
            "jobs": {
                job_id: {"commands": cmds}
                for job_id, cmds in self._jobs.items()
            }
        }
        self._path.write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    def get_commands(self, job_id: str) -> list[str]:
        """Return the list of allowed commands for a job."""
        return list(self._jobs.get(job_id, []))

    def add_command(self, job_id: str, command: str) -> bool:
        """Add a command to a job's allow list. Returns True if newly added."""
        command = command.strip()
        if not command:
            return False
        if job_id not in self._jobs:
            self._jobs[job_id] = []
        if command not in self._jobs[job_id]:
            self._jobs[job_id].append(command)
            self.save()
            return True
        return False

    def remove_command(self, job_id: str, command: str) -> bool:
        """Remove a command from a job's allow list. Returns True if removed."""
        command = command.strip()
        if job_id in self._jobs and command in self._jobs[job_id]:
            self._jobs[job_id].remove(command)
            if not self._jobs[job_id]:
                del self._jobs[job_id]
            self.save()
            return True
        return False

    def clear_job(self, job_id: str) -> int:
        """Remove all commands for a job. Returns count of removed commands."""
        if job_id not in self._jobs:
            return 0
        count = len(self._jobs[job_id])
        del self._jobs[job_id]
        self.save()
        return count

    def is_allowed(self, job_id: str, command: str) -> bool:
        """Check if a command is on the allow list for a job.

        Matching is exact (normalized whitespace). For commands that
        are prefixes of list entries or vice versa, we don't match --
        the user should approve the exact command they expect.
        """
        command = command.strip()
        allowed = self._jobs.get(job_id, [])
        return command in allowed

    def all_jobs(self) -> dict[str, list[str]]:
        """Return a copy of all job allow lists."""
        return {jid: list(cmds) for jid, cmds in self._jobs.items()}
