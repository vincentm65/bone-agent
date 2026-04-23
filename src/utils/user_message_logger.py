"""Lightweight user-message logger for the dream memory system.

Appends one JSONL line per user message, one file per day per project.
Always on by default — no toggle needed.
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# Base directory for daily message logs
CONVERSATIONS_DIR = Path.home() / ".bone" / "conversations"
RETENTION_DAYS = 7


def _project_suffix(project_dir: Path) -> str:
    """Generate a short suffix from a project directory path.

    Format: {dirname}_{first 6 chars of SHA256(path)}
    Avoids collisions between repos with the same folder name.
    """
    path_str = str(project_dir.resolve())
    h = hashlib.sha256(path_str.encode()).hexdigest()[:6]
    return f"{project_dir.name}_{h}"


PROJECT_INDEX_FILE = CONVERSATIONS_DIR / ".project_index.jsonl"


def _register_project(key: str, project_dir: Path) -> None:
    """Append a key→path mapping to the project index if not already present."""
    resolved = str(project_dir.resolve())
    # Check if this key already maps to this path
    if PROJECT_INDEX_FILE.exists():
        with open(PROJECT_INDEX_FILE, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("key") == key and entry.get("path") == resolved:
                    return  # Already indexed
    PROJECT_INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROJECT_INDEX_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"key": key, "path": resolved}) + "\n")


class UserMessageLogger:
    """Logs user messages to daily JSONL files for later dream processing.

    When a project_dir is provided, messages go to a per-project file:
        {date}__{dirname}_{hash}.jsonl
    Without a project_dir, messages go to the catch-all:
        {date}.jsonl
    """

    def __init__(self, conversations_dir: Path | None = None):
        self._dir = conversations_dir or CONVERSATIONS_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    def log_user_message(self, content: str, project_dir: Path | None = None) -> None:
        """Append a single user message to today's JSONL file.

        Args:
            content: The user message text.
            project_dir: Optional project root directory. If provided,
                messages are written to a per-project file.

        Opens in append mode and flushes immediately for crash safety.
        Each message is one self-contained JSON line.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        if project_dir:
            suffix = _project_suffix(project_dir)
            _register_project(suffix, project_dir)
            filepath = self._dir / f"{today}__{suffix}.jsonl"
        else:
            filepath = self._dir / f"{today}.jsonl"
        entry = {"ts": datetime.now().isoformat(), "msg": content}
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    @staticmethod
    def cleanup_old_files(directory: Path | None = None, retention_days: int = RETENTION_DAYS) -> int:
        """Delete JSONL files older than retention_days. Returns count of files removed."""
        target_dir = directory or CONVERSATIONS_DIR
        if not target_dir.exists():
            return 0

        cutoff = datetime.now() - timedelta(days=retention_days)
        removed = 0
        surviving = set()
        for f in target_dir.glob("*.jsonl"):
            if f.stat().st_mtime < cutoff.timestamp():
                f.unlink()
                removed += 1
                logger.debug("Removed old conversation log: %s", f.name)
            else:
                surviving.add(f.name)

        # Prune stale entries from the project index
        index_file = target_dir / ".project_index.jsonl"
        if index_file.exists():
            kept: list[str] = []
            for line in index_file.read_text(encoding="utf-8").splitlines():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = entry.get("key", "")
                # Keep entry if any file matching its key still exists
                if any(key in name for name in surviving):
                    kept.append(line)
            index_file.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")

        return removed
