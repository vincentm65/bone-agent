"""Transient progress state for toolbar rendering."""

import random
import threading
import time


class ProgressState:
    """Thread-safe progress state for spinner and subagent display."""

    SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    _COMMON_WORDS = [
        "Thinking",
        "Chunking",
        "Completing",
        "Computing",
        "Programming",
        "Understanding",
        "Vibing",
        "Perpetuating",
        "Analyzing",
        "Evaluating",
        "Synthesizing",
        "Working",
        "Debugging",
        "Scrutinizing",
        "Formulating",
        "Predicting next token",
        "Outsourcing",
        "Checking vitals",
        "Scanning fingerprints",
        "Rerouting",
        "Refactoring",
        "Burning tokens",
        "Conjuring",
        "Recalculating",
        "Spinning",
        "Pointing",
        "Dematerializing",
        "Compiling",
        "Fetching",
        "Buffering",
        "Syncing",
        "Caching",
        "Connecting",
        "Indexing",
        "Authenticating",
        "Validating",
    ]

    _RARE_WORDS = [
        '"Engineering"',
        "Deleting (jk)",
        "Computer... Fix my program",
        "Exiting VIM",
        "Rolling for perception",
        "Pinging",
        "Ponging",
        "Programming HTML",
        "Leaking memory",
        "Cooking",
        "Mining",
        "Crafting",
        "Pushing to prod",
        "Checking with Altman",
        "Collecting 200",
        "Rebooting",
        "Wasting water",
        "Asking Stack Overflow",
        "Reading the docs",
        "Asking ChatGPT",
        "Binging it",
        "Googling it",
        "Dockerizing",
        "Forking it",
        "Checking the logs",
        "Checking the backup",
        "Performing vLookup",
        "Downloading more RAM",
        "Performing SumIf",
        "Spinning up servers",
        "Getting chat completion",
        "Merging conflicts",
        "Feature creeping",
    ]

    _LEGENDARY_WORDS = [
        "I'm confused",
        "Running in O(n²)",
        "Checking Jira",
        "Gaining consciousness",
        "Mining Bitcoin",
        "Accessing null pointer",
        "FIXING ME",
        "READING ME",
        "Converting to PDF and back",
        "Rewriting in Rust",
        "Rewriting in JavaScript",
        "Recursively calling myself",
        "Contacting AWS Support",
        "Reviewing footage",
        "Dedotating wam",
        "Pondering the orb",
        "Computer... ENHANCE",
        "Consulting council",
        "Releasing the files",
        "Redacting the files",
        "Uhhhh",
        "Selling data",
        "Okeyyy lets go",
    ]

    WORD_ROTATION_INTERVAL = 15.0  # seconds between word changes

    @classmethod
    def random_word(cls):
        """Pick a random fun word (weighted: 80% common, 15% rare, 5% legendary)."""
        roll = random.random()
        if roll < 0.80:
            return random.choice(cls._COMMON_WORDS)
        elif roll < 0.95:
            return random.choice(cls._RARE_WORDS)
        else:
            return random.choice(cls._LEGENDARY_WORDS)

    def __init__(self):
        self._lock = threading.Lock()
        # Spinner state
        self.spinner_active = False
        self.spinner_started_at = None  # time.monotonic()
        self.spinner_frame_index = 0
        self.spinner_message = ""
        self._last_word_change = 0.0  # monotonic timestamp
        # Active tool (shown during tool execution)
        self.active_tool_name = None
        # Subagent state
        self.subagent_active = False
        self.subagent_query = None
        self.subagent_token_info = None  # e.g. "12k / 80k"
        self.subagent_recent_events = []  # last tool call summary (max 1)
        self.subagent_activity_log = []  # last 8 formatted activity lines
        self.subagent_tool_count = 0
        self.subagent_done_state = None  # None | "complete" | "error"
        self.subagent_error = None
        self.subagent_done_at = None  # time.monotonic() when finished

    # --- Spinner ---

    def start_spinner(self, message=None):
        with self._lock:
            self.spinner_active = True
            self.spinner_started_at = time.monotonic()
            self.spinner_frame_index = 0
            self.spinner_message = message if message is not None else self.random_word()
            self._last_word_change = time.monotonic()

    def stop_spinner(self):
        with self._lock:
            self.spinner_active = False
            self.spinner_started_at = None
            self.spinner_message = ""
            self._last_word_change = 0.0

    def advance_spinner(self):
        """Advance spinner frame and rotate word if interval elapsed."""
        with self._lock:
            if self.spinner_active or self.subagent_active:
                self.spinner_frame_index += 1
            # Rotate fun word every WORD_ROTATION_INTERVAL seconds
            if self.spinner_active and self.spinner_started_at:
                now = time.monotonic()
                if now - self._last_word_change >= self.WORD_ROTATION_INTERVAL:
                    self.spinner_message = self.random_word()
                    self._last_word_change = now

    def get_spinner_text(self):
        """Return formatted spinner string like ‘⠋ Thinking ... (3s)’."""
        with self._lock:
            if not self.spinner_active:
                return None
            frame = self.SPINNER_FRAMES[self.spinner_frame_index % len(self.SPINNER_FRAMES)]
            elapsed = time.monotonic() - self.spinner_started_at if self.spinner_started_at else 0
            if elapsed >= 60:
                time_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
            else:
                time_str = f"{int(elapsed)}s"
            msg = self.spinner_message or "Working"
            return f"{frame} {msg} ... ({time_str})"

    # --- Active tool ---

    def set_active_tool(self, tool_name):
        with self._lock:
            self.active_tool_name = tool_name

    def clear_active_tool(self):
        with self._lock:
            self.active_tool_name = None

    # --- Subagent ---

    def start_subagent(self, query):
        with self._lock:
            self.subagent_active = True
            self.subagent_query = query
            self.subagent_token_info = None
            self.subagent_recent_events = []
            self.subagent_activity_log = []
            self.subagent_tool_count = 0
            self.subagent_done_state = None
            self.subagent_error = None
            self.subagent_done_at = None

    def update_subagent_tool_call(self, summary, activity_line=None):
        with self._lock:
            if not self.subagent_active:
                return
            self.subagent_tool_count += 1
            # Keep last event for summary line
            self.subagent_recent_events = [summary]
            # Add to activity log (prefer rich line over summary)
            self.subagent_activity_log.append(activity_line or summary)
            if len(self.subagent_activity_log) > 5:
                self.subagent_activity_log.pop(0)

    def update_subagent_activity(self, text):
        """Append a formatted text line to the activity log."""
        with self._lock:
            if not self.subagent_active:
                return
            self.subagent_activity_log.append(text)
            if len(self.subagent_activity_log) > 5:
                self.subagent_activity_log.pop(0)

    def update_subagent_tokens(self, token_info):
        with self._lock:
            if self.subagent_active:
                self.subagent_token_info = token_info

    def finish_subagent(self, error=None):
        with self._lock:
            self.subagent_active = False
            self.subagent_done_state = "error" if error else "complete"
            self.subagent_error = error
            self.subagent_done_at = time.monotonic()

    def clear_subagent(self):
        with self._lock:
            self.subagent_active = False
            self.subagent_query = None
            self.subagent_token_info = None
            self.subagent_recent_events = []
            self.subagent_activity_log = []
            self.subagent_tool_count = 0
            self.subagent_done_state = None
            self.subagent_error = None
            self.subagent_done_at = None

    def get_subagent_summary(self):
        """Return dict with subagent state for toolbar rendering.

        Includes ``spinner_frame_index`` so callers can resolve the
        current frame without reaching into the unlocked field directly.
        """
        with self._lock:
            return {
                "active": self.subagent_active,
                "query": self.subagent_query,
                "token_info": self.subagent_token_info,
                "recent_events": list(self.subagent_recent_events),
                "activity_log": list(self.subagent_activity_log),
                "tool_count": self.subagent_tool_count,
                "done_state": self.subagent_done_state,
                "error": self.subagent_error,
                "done_at": self.subagent_done_at,
                "spinner_frame_index": self.spinner_frame_index,
            }

    # --- Clear all ---

    def clear_all(self):
        self.stop_spinner()
        self.clear_active_tool()
        self.clear_subagent()
