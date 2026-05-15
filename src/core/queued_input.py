"""Queued user-message input buffering for the agentic loop."""

import queue
import threading
from typing import Any, Callable, Optional


class QueuedInput:
    """Thread-safe buffer for user messages submitted while an agent turn is running."""

    def __init__(self, on_change: Optional[Callable[[], None]] = None):
        """
        Args:
            on_change: Optional callback invoked when the queue state changes
                       (e.g., to invalidate a toolbar). Called after enqueue,
                       drain (if items drained), and clear (if items cleared).
        """
        self._queue: queue.Queue[Any] = queue.Queue()
        self._lock = threading.Lock()
        self._agent_running_event = threading.Event()
        self._on_change = on_change

    # -- Agent-running guard --------------------------------------------------

    @property
    def agent_running_event(self) -> threading.Event:
        """Expose the underlying event for external wait/set."""
        return self._agent_running_event

    def set_agent_running(self, running: bool) -> None:
        """Set or clear the agent-running flag."""
        if running:
            self._agent_running_event.set()
        else:
            self._agent_running_event.clear()

    def is_agent_running(self) -> bool:
        """Return True if an agent turn is currently in progress."""
        return self._agent_running_event.is_set()

    # -- Queue operations -----------------------------------------------------

    def enqueue(self, content: Any) -> bool:
        """Buffer a user message for the next turn. Rejects empty/whitespace strings."""
        if content is None:
            return False
        if isinstance(content, str):
            if not content.strip():
                return False
        with self._lock:
            self._queue.put(content)
        if self._on_change is not None:
            self._on_change()
        return True

    def count(self) -> int:
        """Return the number of queued user messages (thread-safe)."""
        with self._lock:
            return self._queue.qsize()

    def has_items(self) -> bool:
        """Return True if there are queued user messages waiting."""
        return self.count() > 0

    def drain(self, limit: int | None = None) -> list[Any]:
        """Drain queued user messages in FIFO order."""
        with self._lock:
            drained = self._drain_queue(self._queue, limit)
        if drained and self._on_change is not None:
            self._on_change()
        return drained

    def clear(self) -> int:
        """Remove all queued user messages and return the count removed."""
        with self._lock:
            count = len(self._drain_queue(self._queue))
        if count and self._on_change is not None:
            self._on_change()
        return count

    # -- Private helpers ------------------------------------------------------

    @staticmethod
    def _drain_queue(q: queue.Queue, limit: int | None = None) -> list:
        """Drain up to *limit* items from *q* into a list (0 on empty)."""
        items: list = []
        while True:
            if limit is not None and len(items) >= limit:
                break
            try:
                items.append(q.get_nowait())
            except Exception:
                break
        return items
