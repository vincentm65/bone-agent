"""ThinkingIndicator — Rich spinner with rotating messages and elapsed time."""

import os
import random
import sys
import threading
import time

class ThinkingIndicator:
    """Simple spinner wrapper that always cleans up."""

    def __init__(self, console, message="Thinking ...", spinner="dots"):
        self.console = console
        self.message = message
        self.spinner = spinner
        self._last_word_change = 0
        self._word_change_interval = 15.0  # Change word every 15 seconds
        
        self._common_words = [
            "Thinking ...",
            "Chunking ...",
            "Completing ...",
            "Computing ...",
            "Programming ...",
            "Understanding ...",
            "Vibing ...",
            "Perpetuating ...",
            "Analyzing ...",
            "Evaluating ...",
            "Synthesizing ...",
            "Working ...",
            "Debugging ...",
            "Scrutinizing ...",
            "Formulating ...",
            "Predicting next token ...",
            "Outsourcing ...",
            "Checking vitals ...",
            "Scanning fingerprints ...",
            "Rerouting ...",
            "Refactoring ...",
            "Burning tokens ...",
            "Conjuring ...",
            "Recalculating ...",
            "Spinning ...",
            "Pointing ...",
            "Dematerializing ...",
            "Compiling ...",
            "Fetching ...",
            "Buffering ...",
            "Syncing ...",
            "Caching ...",
            "Connecting ...",
            "Indexing ...",
            "Authenticating ...",
            "Validating ...",
        ]

        self._rare_words = [
            '"Engineering" ...',
            "Deleting (jk) ...",
            "Computer... Fix my program ...",
            "Exiting VIM ...",
            "Rolling for perception ...",
            "Pinging ...",
            "Ponging ...",
            "Programming HTML ...",
            "Leaking memory ...",
            "Cooking ...",
            "Mining ...",
            "Crafting ...",
            "Pushing to prod ...",
            "Checking with Altman ...",
            "Collecting 200 ...",
            "Rebooting...",
            "Wasting water ...",
            "Asking Stack Overflow ...",
            "Reading the docs ...",
            "Asking ChatGPT ...",
            "Binging it ...",
            "Googling it ...",
            "Dockerizing ...",
            "Forking it ...",
            "Checking the logs ...",
            "Checking the backup ...",
            "Performing vLookup ...",
            "Downloading more RAM ...",
            "Performing SumIf ...",
            "Spinning up servers ...",
            "Getting chat completion ...",
            "Merging conflicts ...",
            "Feature creeping ...",
        ]

        self._legendary_words = [
            "I'm confused ...",
            "Running in O(n²) ...",
            "Checking Jira ...",
            "Gaining consciousness ...",
            "Mining Bitcoin ...",
            "Accessing null pointer ...",
            "FIXING ME ...",
            "READING ME ...",
            "Converting to PDF and back ...",
            "Rewriting in Rust ...",
            "Rewriting in JavaScript ...",
            "Recursively calling myself ...",
            "Contacting AWS Support ...",
            "Reviewing footage ...",
            "Dedotating wam ...",
            "Pondering the orb ...",
            "Computer... ENHANCE ...",
            "Consulting council ...",
            "Releasing the files ...",
            "Redacting the files ...",
            "Uhhhh ...",
            "Selling data ...",
            "Okeyyy lets go ...",
        ]
        self._status = None
        self._active = False
        self._start_time = None
        self._timer_thread = None
        self._stop_timer = threading.Event()
        self._elapsed_before_pause = 0.0
        self._has_been_started = False
        self._saved_termios = None

    def _select_random_word(self):
        """Select a random word from weighted word lists."""
        roll = random.random()
        
        if roll < 0.80:
            return random.choice(self._common_words)
        elif roll < 0.95:
            return random.choice(self._rare_words)
        else:
            return random.choice(self._legendary_words)

    @staticmethod
    def _format_time(seconds):
        """Format seconds as whole seconds or minutes:seconds."""
        if seconds >= 60:
            mins = int(seconds // 60)
            secs = int(seconds % 60)
            return f"{mins}m {secs}s"
        else:
            return f"{int(seconds)}s"

    @staticmethod
    def _set_raw_mode():
        """Switch stdin to raw mode to prevent keystroke echoes during spinner."""
        if os.name == 'nt':
            return
        try:
            import termios
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            new = old.copy()
            # lflag: disable ECHO, ICANON (line buffering), IEXTEN
            new[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN)
            # iflag: disable ICRNL (map CR to NL) so Enter doesn't produce newline
            new[0] &= ~(termios.ICRNL)
            termios.tcsetattr(fd, termios.TCSANOW, new)
            return old
        except Exception:
            return None

    @staticmethod
    def _restore_terminal_mode(saved):
        """Restore terminal mode from saved termios attributes."""
        if os.name == 'nt' or saved is None:
            return
        try:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, saved)
        except Exception:
            pass

    def start(self):
        # Select initial word
        self.message = self._select_random_word()
        
        # Initialize timer (reset only on first start)
        if not self._has_been_started:
            self._elapsed_before_pause = 0.0
            self._has_been_started = True
            self._last_word_change = 0
        
        self._start_time = time.time()
        self._stop_timer.clear()
        
        # Always recreate and restart status with new message
        if self._status and self._active:
            self._status.stop()
        self._saved_termios = self._set_raw_mode()
        self._status = self.console.status(self.message, spinner=self.spinner, spinner_style="#5F9EA0")
        self._status.start()
        self._active = True
        
        # Start background timer thread
        self._timer_thread = threading.Thread(target=self._update_timer, daemon=True)
        self._timer_thread.start()
    
    def _update_timer(self):
        """Background thread: update status message with elapsed time."""
        while not self._stop_timer.is_set() and self._status and self._active:
            # Calculate elapsed time including previous pauses
            elapsed = self._elapsed_before_pause + (time.time() - self._start_time)

            # Change word every 15 seconds
            if elapsed - self._last_word_change >= self._word_change_interval:
                self.message = self._select_random_word()
                self._last_word_change = elapsed

            # Format elapsed time (e.g., "Thinking ... (1s)" or "Thinking ... (1m 30s)")
            time_str = f"({self._format_time(elapsed)})"
            updated_message = f"{self.message} {time_str}"

            # Update the status message
            if self._status:
                self._status.update(updated_message)
            
            self._stop_timer.wait(0.1)  # Update every 100ms

    def stop(self, reset=False):
        """Stop the thinking indicator.

        Args:
            reset: If True, reset elapsed time and state for next use cycle.
        """
        # Calculate and store elapsed time (including accumulated pauses)
        elapsed_time = None
        if self._start_time:
            elapsed_time = self._elapsed_before_pause + (time.time() - self._start_time)
            self._elapsed_before_pause = elapsed_time
        
        # Stop timer thread first (close race window before stopping status)
        self._active = False
        self._stop_timer.set()
        if self._timer_thread:
            self._timer_thread.join(timeout=0.5)
        
        if self._status:
            self._status.stop()
            self._status = None
        
        # Restore terminal mode (must happen after status.stop() so Rich
        # cursor cleanup runs in raw mode, then we hand control back to ptk)
        self._restore_terminal_mode(self._saved_termios)
        self._saved_termios = None
        
        # Reset state for next use cycle
        if reset:
            self._has_been_started = False
            self._elapsed_before_pause = 0.0
        
        self._start_time = None

    def pause(self):
        # Stop without showing completion time (accumulates elapsed time)
        self.stop(reset=False)

    def resume(self):
        # Resume with timer continuing from accumulated time
        self.start()


