"""Cron scheduler for bone-agent.

Provides natural-language scheduled job execution integrated into the
bone-agent agentic loop. Jobs are defined in ~/.bone/cron/jobs.yaml and
run as background threads while bone-agent is active.

External trigger: bone-agent --cron-run <job-id>
"""

import logging
import re
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────

def _get_cron_dir() -> Path:
    """Return ~/.bone/cron/ directory, creating it if needed."""
    cron_dir = Path.home() / ".bone" / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    return cron_dir


def _get_jobs_path() -> Path:
    return _get_cron_dir() / "jobs.yaml"


def _get_log_dir() -> Path:
    log_dir = _get_cron_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


# ── Data model ───────────────────────────────────────────────────────────

@dataclass
class CronJob:
    """A single cron job definition."""
    id: str
    schedule: str           # Natural language: "every 5 minutes", "weekdays at 8am"
    command: str            # The prompt to feed into the agentic loop
    enabled: bool = True
    description: str = ""
    last_run: Optional[str] = None    # ISO timestamp of last successful run
    last_status: Optional[str] = None  # "ok" | "error"
    created: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CronJob":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ── Schedule parser ──────────────────────────────────────────────────────

# Patterns we support (ordered by specificity):
#   "every N minutes|hours|days"
#   "daily at HH:MM"
#   "weekdays at HH:MM"
#   "mondays|tuesdays|... at HH:MM"
#   "HH:MM" (daily shorthand)

def _extract_time(m: re.Match) -> dict:
    """Extract hour/minute from a regex match with hour, minute, ampm groups."""
    hour = int(m.group("hour"))
    minute = int(m.group("minute") or 0)
    ampm = (m.group("ampm") or "").lower()
    if ampm == "am" and hour == 12:
        hour = 0
    elif ampm == "pm" and hour != 12:
        hour += 12
    return {"hour": hour, "minute": minute}


_SCHEDULE_PATTERNS = [
    # every N <unit>
    (re.compile(
        r"^every\s+(?P<n>\d+)\s*(?P<unit>minute|minutes|min|m|hour|hours|hr|h|day|days|d)s?\s*$",
        re.IGNORECASE
    ), "interval"),
    # every day/night/morning/afternoon/evening [at] <time>
    (re.compile(
        r"^every\s+(?:day|night|morning|afternoon|evening)s?\s+(?:at\s+)?(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap]m)?\s*$",
        re.IGNORECASE
    ), "daily"),
    # weekdays at <time>
    (re.compile(
        r"^weekdays?\s+at\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap]m)?\s*$",
        re.IGNORECASE
    ), "weekdays"),
    # specific day at <time>
    (re.compile(
        r"^(?P<day>monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\s+at\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap]m)?\s*$",
        re.IGNORECASE
    ), "day_of_week"),
    # daily at <time>
    (re.compile(
        r"^daily\s+at\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap]m)?\s*$",
        re.IGNORECASE
    ), "daily"),
    # bare HH:MM or HHam/pm (treated as daily)
    (re.compile(
        r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>[ap]m)?\s*$"
    ), "daily"),
]

_DAY_MAP = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def parse_schedule(schedule: str) -> dict:
    """Parse a natural-language schedule into a structured spec.

    Returns dict with:
        type: "interval" | "daily" | "weekdays" | "day_of_week"
        For interval: interval_seconds (int)
        For time-based: hour (int), minute (int)
        For day_of_week: weekday (0=Mon..6=Sun)

    Raises ValueError if schedule can't be parsed.
    """
    schedule = schedule.strip()
    for pattern, sched_type in _SCHEDULE_PATTERNS:
        m = pattern.match(schedule)
        if m:
            if sched_type == "interval":
                n = int(m.group("n"))
                if n <= 0:
                    raise ValueError(
                        f"Interval must be at least 1: 'every {n} {m.group('unit')}'"
                    )
                unit = m.group("unit").lower()
                if unit in ("minute", "minutes", "min", "m"):
                    return {"type": "interval", "interval_seconds": n * 60}
                elif unit in ("hour", "hours", "hr", "h"):
                    return {"type": "interval", "interval_seconds": n * 3600}
                elif unit in ("day", "days", "d"):
                    return {"type": "interval", "interval_seconds": n * 86400}
            elif sched_type == "weekdays":
                t = _extract_time(m)
                return {"type": "weekdays", **t}
            elif sched_type == "day_of_week":
                t = _extract_time(m)
                return {
                    "type": "day_of_week",
                    "weekday": _DAY_MAP[m.group("day").lower()],
                    **t,
                }
            elif sched_type == "daily":
                t = _extract_time(m)
                return {"type": "daily", **t}

    raise ValueError(
        f"Cannot parse schedule: '{schedule}'. "
        f"Examples: 'every 5 minutes', 'every hour', 'daily at 8am', "
        f"'every day at 5am', 'weekdays at 9:00', 'mondays at 10:30pm'"
    )


def _should_run(spec: dict, last_run: Optional[datetime], now: datetime) -> bool:
    """Check if a job with the given schedule spec should run now."""
    if spec["type"] == "interval":
        interval = spec["interval_seconds"]
        if last_run is None:
            return True
        return (now - last_run).total_seconds() >= interval

    elif spec["type"] in ("daily", "weekdays", "day_of_week"):
        # Time-based: check if we've passed the target time today
        # and haven't already run today
        target_time = now.replace(hour=spec["hour"], minute=spec["minute"], second=0, microsecond=0)

        # Check day-of-week constraints
        if spec["type"] == "weekdays" and now.weekday() >= 5:
            return False
        if spec["type"] == "day_of_week" and now.weekday() != spec["weekday"]:
            return False

        # Has the target time passed today?
        if now < target_time:
            return False

        # Did we already run today (after target time)?
        if last_run is not None and last_run >= target_time:
            return False

        return True

    return False


# ── Config persistence ───────────────────────────────────────────────────

class CronConfig:
    """Load/save cron jobs from ~/.bone/cron/jobs.yaml."""

    def __init__(self):
        self._path = _get_jobs_path()
        self.jobs: dict[str, CronJob] = {}
        self.load()

    def load(self):
        self.jobs.clear()
        if not self._path.exists():
            return
        try:
            data = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
            for job_dict in data.get("jobs", []):
                job = CronJob.from_dict(job_dict)
                self.jobs[job.id] = job
        except Exception as e:
            logger.warning("Failed to load cron config: %s", e)

    def save(self):
        data = {"jobs": [j.to_dict() for j in self.jobs.values()]}
        self._path.write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    def add_job(self, job: CronJob):
        self.jobs[job.id] = job
        self.save()

    def remove_job(self, job_id: str) -> bool:
        if job_id in self.jobs:
            del self.jobs[job_id]
            self.save()
            return True
        return False

    def get_job(self, job_id: str) -> Optional[CronJob]:
        return self.jobs.get(job_id)

    def update_job(self, job_id: str, **kwargs):
        job = self.jobs.get(job_id)
        if job:
            for k, v in kwargs.items():
                if k in job.__dataclass_fields__:
                    setattr(job, k, v)
            self.save()


# ── Scheduler ────────────────────────────────────────────────────────────

def _write_job_log(job: CronJob, output: str, error: bool):
    """Append job output to a log file."""
    log_dir = _get_log_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"{job.id}_{timestamp}.log"
    try:
        log_file.write_text(
            f"Job: {job.id}\n"
            f"Schedule: {job.schedule}\n"
            f"Ran at: {datetime.now().isoformat()}\n"
            f"Status: {'ERROR' if error else 'OK'}\n"
            f"{'─' * 40}\n"
            f"{output}\n",
            encoding="utf-8",
        )
    except Exception as e:
        logger.error("Failed to write cron log: %s", e)


# ── Dream job (auto-seeded) ─────────────────────────────────────────────

DREAM_JOB_ID = "dream"
DREAM_JOB_SCHEDULE = "daily at 4am"


def ensure_dream_job(config: CronConfig) -> None:
    """Sync the dream memory job with the DREAM_SETTINGS.enabled config.

    - Enabled and missing  → seed the job
    - Enabled and present  → no-op
    - Disabled and present → remove the job
    - Disabled and missing → no-op
    """
    from utils.settings import dream_settings
    from llm.config import MEMORY_SETTINGS

    if dream_settings.enabled and MEMORY_SETTINGS.get("enabled", True):
        if DREAM_JOB_ID in config.jobs:
            return
        job = CronJob(
            id=DREAM_JOB_ID,
            schedule=DREAM_JOB_SCHEDULE,
            command="Run the dream memory consolidation process. Read yesterday's user messages from ~/.bone/conversations/, analyze them for user preferences, constraints, and explicit 'remember this' requests — do NOT record activity history or what the user was working on. Consolidate into memory files. Then clean up JSONL files older than 7 days.",
            enabled=True,
            description="Dream memory consolidation — scans user messages and updates memories",
        )
        config.add_job(job)
        logger.info("Seeded dream memory cron job (daily at 4am)")
    else:
        if DREAM_JOB_ID in config.jobs:
            config.remove_job(DREAM_JOB_ID)
            logger.info("Removed dream memory cron job (disabled in config)")


def run_single_job(job: CronJob, console=None, interactive=False) -> None:
    """Execute a single cron job without requiring a CronScheduler instance.

    Used by the /cron run subcommand (interactive=True) and run_job_headless
    (interactive=False, default).

    Args:
        job: The CronJob to execute.
        console: Optional Rich console for interactive output.
        interactive: If True, use the real console for interactive command
            approval (test-run mode). Commands are auto-saved to the allow list.
            If False, use a buffer console (scheduled mode). Unlisted commands
            are blocked.
    """
    from rich.console import Console as RichConsole
    from io import StringIO
    from core.cron_allowlist import CronAllowlist

    # Capture output for logging
    output_buf = StringIO()

    if interactive and console is not None:
        # Interactive test run: use the real console so user can approve commands
        job_console = console
    else:
        # Scheduled run: use a buffer console (no interactive prompts)
        job_console = RichConsole(
            file=output_buf,
            force_terminal=True,
            width=80,
        )

    try:
        from core.chat_manager import ChatManager
        from core.agentic import AgenticOrchestrator
        from utils.paths import RG_EXE_PATH
        from llm.config import TOOLS_ENABLED

        if not TOOLS_ENABLED:
            raise RuntimeError("Cron requires tools to be enabled")

        # Fresh ChatManager for this job
        chat_manager = ChatManager()

        # Dream job: auto-approve edits and run cleanup before agent starts
        if job.id == DREAM_JOB_ID:
            chat_manager.approve_mode = "accept_edits"
            from utils.user_message_logger import UserMessageLogger
            removed = UserMessageLogger.cleanup_old_files()
            if removed:
                logger.info("Dream job: removed %d old JSONL files", removed)

        # Build the prompt — load dream.md for dream job, else use command field
        if job.id == DREAM_JOB_ID:
            dream_prompt_path = Path(__file__).resolve().parents[2] / "prompts" / "main" / "dream.md"
            if dream_prompt_path.is_file():
                command_text = dream_prompt_path.read_text(encoding="utf-8").strip()
            else:
                command_text = job.command
        else:
            command_text = job.command

        prompt = (
            f"[Cron job: {job.id}]\n"
            f"{command_text}"
        )

        repo_root = Path.cwd().resolve()

        # Set up cron allow list for command gating
        allowlist = CronAllowlist()

        orchestrator = AgenticOrchestrator(
            chat_manager=chat_manager,
            repo_root=repo_root,
            rg_exe_path=RG_EXE_PATH,
            console=job_console,
            debug_mode=False,
            suppress_result_display=False,
            cron_job_id=job.id,
            cron_allowlist=allowlist,
            cron_interactive=interactive,
        )
        orchestrator.run(prompt)

        # Log output
        _write_job_log(job, output_buf.getvalue(), error=False)

    except Exception as e:
        _write_job_log(job, str(e), error=True)
        raise


class CronScheduler:
    """Background scheduler that runs cron jobs via the agentic loop.

    Starts a daemon thread that wakes every 30 seconds to check if any
    jobs are due. When a job fires, it creates a fresh ChatManager
    (to avoid polluting the user's conversation) and runs the job's
    command through the agentic orchestrator.
    """

    CHECK_INTERVAL = 30  # seconds between schedule checks

    def __init__(self, console=None):
        self.config = CronConfig()
        self.console = console
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._running = False

        # Auto-seed the dream memory job if it doesn't exist
        ensure_dream_job(self.config)

    def start(self):
        """Start the cron scheduler background thread."""
        enabled_jobs = [j for j in self.config.jobs.values() if j.enabled]

        # Validate all schedules on startup
        for job in enabled_jobs:
            try:
                parse_schedule(job.schedule)
            except ValueError as e:
                logger.warning("Cron job '%s' has invalid schedule: %s", job.id, e)

        self._stop_event.clear()
        self._thread = None
        try:
            self._running = True
            self._thread = threading.Thread(
                target=self._run_loop,
                name="cron-scheduler",
                daemon=True,
            )
            self._thread.start()
        except Exception:
            self._running = False
            self._thread = None
            raise
        logger.info("Cron scheduler started with %d job(s)", len(enabled_jobs))

    def stop(self):
        """Signal the scheduler thread to stop and wait for it."""
        self._stop_event.set()
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("Cron scheduler stopped")

    def reload(self):
        """Reload config from disk (e.g. after /cron add/remove)."""
        with self._lock:
            self.config.load()

    def execute_job(self, job: CronJob):
        """Execute a single cron job. Public wrapper around run_single_job."""
        run_single_job(job, console=self.console)

    def _run_loop(self):
        """Main scheduler loop — runs in background thread."""
        # Track last run times from persisted state
        last_runs: dict[str, datetime] = {}
        for job in self.config.jobs.values():
            if job.last_run:
                try:
                    last_runs[job.id] = datetime.fromisoformat(job.last_run)
                except (ValueError, TypeError):
                    pass

        while not self._stop_event.is_set():
            now = datetime.now()

            # Collect due jobs under lock, then execute outside
            due_jobs: list[CronJob] = []
            with self._lock:
                for job in list(self.config.jobs.values()):
                    if not job.enabled:
                        continue
                    try:
                        spec = parse_schedule(job.schedule)
                    except ValueError:
                        continue

                    last_run = last_runs.get(job.id)
                    if _should_run(spec, last_run, now):
                        due_jobs.append(job)

            # Execute jobs outside the lock so scheduling isn't blocked
            for job in due_jobs:
                logger.info("Cron firing job '%s'", job.id)
                try:
                    self.execute_job(job)
                    job.last_run = now.isoformat()
                    job.last_status = "ok"
                    last_runs[job.id] = now
                except Exception as e:
                    logger.error("Cron job '%s' failed: %s", job.id, e)
                    job.last_status = "error"
                    job.last_run = now.isoformat()
                    last_runs[job.id] = now
                finally:
                    with self._lock:
                        # Snapshot only the current job's updated state
                        lr, ls = job.last_run, job.last_status

                        # Reload to pick up any /cron changes made while
                        # the job was running, so we don't overwrite them
                        self.config.load()

                        # Merge our last_run/last_status back onto reloaded job
                        reloaded = self.config.jobs.get(job.id)
                        if reloaded:
                            reloaded.last_run = lr
                            reloaded.last_status = ls

                        self.config.save()

            self._stop_event.wait(self.CHECK_INTERVAL)

            # Reload config from disk so /cron add/remove changes are picked up
            with self._lock:
                self.config.load()
                # Sync in-memory last_runs from reloaded config
                # (picks up /cron run or --cron-run updates)
                for job in self.config.jobs.values():
                    if job.id not in last_runs and job.last_run:
                        try:
                            last_runs[job.id] = datetime.fromisoformat(job.last_run)
                        except (ValueError, TypeError):
                            pass


# ── External runner (for --cron-run) ────────────────────────────────────

def run_job_headless(job_id: str) -> int:
    """Run a single job headlessly (no interactive session).

    Used by `bone-agent --cron-run <job-id>`.

    Returns 0 on success, 1 on failure.
    """
    config = CronConfig()
    job = config.get_job(job_id)
    if not job:
        print(f"Error: cron job '{job_id}' not found")
        return 1

    print(f"Running cron job: {job.id}")
    print(f"Schedule: {job.schedule}")
    print(f"Command: {job.command}")
    print("─" * 40)

    try:
        run_single_job(job)
        job.last_run = datetime.now().isoformat()
        job.last_status = "ok"
        config.save()
        print("─" * 40)
        print("Job completed successfully.")
        return 0
    except Exception as e:
        job.last_run = datetime.now().isoformat()
        job.last_status = "error"
        config.save()
        print(f"─" * 40)
        print(f"Job failed: {e}")
        return 1
