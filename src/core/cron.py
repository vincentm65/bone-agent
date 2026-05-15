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
    schedule: str           # Structured: "interval 5m", "daily 08:00", "weekdays 09:00", "weekly 10:00 mon"
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


# ── Schedule handling ────────────────────────────────────────────────────
#
# Structured schedule formats (written by the LLM directly into jobs.yaml):
#   "interval 5m"         — every 5 minutes (also h, d)
#   "daily 08:00"         — every day at 08:00
#   "weekdays 09:00"      — Mon-Fri at 09:00
#   "weekly 10:00 mon"    — every Monday at 10:00

_INTERVAL_RE = re.compile(r"^interval\s+(?P<n>\d+)(?P<unit>[mhd])$")
_TIME_PATTERN = r"(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)"
_TIME_RE = re.compile(rf"^{_TIME_PATTERN}$")
_WEEKLY_RE = re.compile(
    rf"^weekly\s+{_TIME_PATTERN}\s+(?P<day>mon|tue|wed|thu|fri|sat|sun)$"
)
_DAY_ABBREV = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def parse_schedule(schedule: str) -> dict:
    """Parse a structured schedule string into a spec dict.

    Returns dict with type and relevant fields.
    Raises ValueError if schedule can't be parsed.
    """
    s = schedule.strip().lower()

    # interval 5m / 2h / 1d
    m = _INTERVAL_RE.match(s)
    if m:
        n = int(m.group("n"))
        if n <= 0:
            raise ValueError(f"Interval must be at least 1: '{schedule}'")
        unit = m.group("unit")
        secs = {"m": 60, "h": 3600, "d": 86400}[unit] * n
        return {"type": "interval", "interval_seconds": secs}

    # weekly HH:MM day
    m = _WEEKLY_RE.match(s)
    if m:
        result = {
            "type": "weekly",
            "hour": int(m.group("hour")),
            "minute": int(m.group("minute")),
            "weekday": _DAY_ABBREV[m.group("day")],
        }
        _validate_time_spec(result, schedule)
        return result

    # daily HH:MM / weekdays HH:MM
    parts = s.split()
    if len(parts) == 2 and parts[0] in ("daily", "weekdays"):
        kind = parts[0]
        tm = _TIME_RE.match(parts[1])
        if tm:
            result = {
                "type": kind,
                "hour": int(tm.group("hour")),
                "minute": int(tm.group("minute")),
            }
            _validate_time_spec(result, schedule)
            return result

    raise ValueError(
        f"Cannot parse schedule: '{schedule}'. "
        f"Formats: 'interval 5m', 'interval 2h', 'interval 1d', "
        f"'daily 08:00', 'weekdays 09:00', 'weekly 10:00 mon'"
    )


def _validate_time_spec(spec: dict, schedule: str) -> None:
    """Validate hour/minute ranges in a parsed time-based schedule spec."""
    if spec["type"] in ("daily", "weekdays", "weekly"):
        hour = spec.get("hour", 0)
        minute = spec.get("minute", 0)
        if not (0 <= hour <= 23):
            raise ValueError(f"Invalid hour {hour} in schedule: '{schedule}'")
        if not (0 <= minute <= 59):
            raise ValueError(f"Invalid minute {minute} in schedule: '{schedule}'")


def _should_run(spec: dict, last_run: Optional[datetime], now: datetime) -> bool:
    """Check if a job with the given schedule spec should run now."""
    if spec["type"] == "interval":
        if last_run is None:
            return True
        return (now - last_run).total_seconds() >= spec["interval_seconds"]

    # Time-based: daily, weekdays, weekly
    target = now.replace(hour=spec["hour"], minute=spec["minute"], second=0, microsecond=0)

    if spec["type"] == "weekdays" and now.weekday() >= 5:
        return False
    if spec["type"] == "weekly" and now.weekday() != spec["weekday"]:
        return False

    if now < target:
        return False
    if last_run is not None and last_run >= target:
        return False
    return True


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
DREAM_JOB_SCHEDULE = "daily 04:00"


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
            existing = config.jobs[DREAM_JOB_ID]
            try:
                parse_schedule(existing.schedule)
            except ValueError:
                logger.info("Upgrading dream job schedule from '%s' to '%s'",
                            existing.schedule, DREAM_JOB_SCHEDULE)
                existing.schedule = DREAM_JOB_SCHEDULE
                config.save()
            return
        job = CronJob(
            id=DREAM_JOB_ID,
            schedule=DREAM_JOB_SCHEDULE,
            command=(
                "Run the dream memory consolidation process. Read yesterday's user messages from ~/.bone/conversations/ and do two things:\n"
                "\n"
                "1. Memory consolidation: Analyze messages for user preferences, constraints, and explicit 'remember this' requests — do NOT record activity history or what the user was working on. Consolidate into memory files (~/.bone/user_memory.md for global preferences, .bone/agents.md in the project root for project-specific preferences). Keep each under 1500 chars.\n"
                "\n"
                "2. Skill auto-creation: Look for reusable patterns across conversations — repeated multi-step workflows the user asks for frequently, consistent styles or formats they prefer, or instructions they give more than once. When you find a clear pattern, create or update a skill file in ~/.bone/skills/ using the skill format (YAML frontmatter with description and tags, then a # heading and body). Only create skills for genuinely reusable workflows — not one-off requests or trivia. Each skill should be a concise, self-contained instruction prompt.\n"
                "\n"
                "Then clean up JSONL files older than 7 days."
            ),
            enabled=True,
            description="Dream memory consolidation and skill auto-creation — scans user messages, updates memories, and creates reusable skills",
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
        """Reload config from disk (e.g. after /cron remove or external edit)."""
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

            # Reload config from disk so external changes are picked up
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
