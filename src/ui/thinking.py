"""ThinkingIndicator — Rich spinner with rotating messages and elapsed time."""

import threading
import time

from .status_state import ProgressState

SPINNER_REFRESH_INTERVAL = 0.1


class ThinkingIndicator:
    """Simple spinner wrapper that always cleans up."""

    def __init__(self, console, message="Thinking ...", spinner="dots", chat_manager=None):
        self.console = console
        self.message = message
        self.spinner = spinner
        self.chat_manager = chat_manager
        self._last_word_change = 0
        self._word_change_interval = ProgressState.WORD_ROTATION_INTERVAL
        self._status = None
        self._active = False
        self._start_time = None
        self._timer_thread = None
        self._stop_timer = threading.Event()
        self._elapsed_before_pause = 0.0
        self._has_been_started = False

    @staticmethod
    def _select_random_word():
        """Select a random word from weighted word lists."""
        return ProgressState.random_word()

    @staticmethod
    def _format_time(seconds):
        """Format seconds as whole seconds or minutes:seconds."""
        if seconds >= 60:
            mins = int(seconds // 60)
            secs = int(seconds % 60)
            return f"{mins}m {secs}s"
        else:
            return f"{int(seconds)}s"

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
        self._active = True
        
        # Update toolbar progress state
        if self.chat_manager and hasattr(self.chat_manager, 'progress'):
            self.chat_manager.progress.start_spinner(self.message)
        
        # Start background timer thread (for message rotation + toolbar invalidation)
        self._timer_thread = threading.Thread(target=self._update_timer, daemon=True)
        self._timer_thread.start()
    
    def _update_timer(self):
        """Background thread: update progress state and rotate words."""
        while not self._stop_timer.is_set() and self._active:
            # Calculate elapsed time including previous pauses
            elapsed = self._elapsed_before_pause + (time.time() - self._start_time)

            # Change word every 15 seconds
            if elapsed - self._last_word_change >= self._word_change_interval:
                self.message = self._select_random_word()
                self._last_word_change = elapsed

            # Update progress state
            if self.chat_manager and hasattr(self.chat_manager, 'progress'):
                self.chat_manager.progress.advance_spinner()
                self.chat_manager.progress.spinner_message = self.message
                self.chat_manager.invalidate_toolbar()
            
            self._stop_timer.wait(SPINNER_REFRESH_INTERVAL)

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
        
        self._active = False
        self._stop_timer.set()
        if self._timer_thread:
            self._timer_thread.join(timeout=0.5)
        
        # Clear toolbar progress state
        if self.chat_manager and hasattr(self.chat_manager, 'progress'):
            self.chat_manager.progress.stop_spinner()
        
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
