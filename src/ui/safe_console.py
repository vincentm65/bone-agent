"""Thread-safe console wrapper for Rich output during PTK-managed agent work.

When a PTK application is active, routes console output through
prompt_toolkit's terminal-yield context to avoid toolbar collisions.
Falls back to direct console.print() when no PTK app is running.
"""

import asyncio
import threading
import concurrent.futures

from prompt_toolkit.application import in_terminal, run_in_terminal

_TERMINAL_PRINT_TIMEOUT_SECONDS = 5


def _wait_for_future(future):
    """Wait briefly for concurrent futures; asyncio futures use nonblocking check."""
    if isinstance(future, concurrent.futures.Future):
        future.result(timeout=_TERMINAL_PRINT_TIMEOUT_SECONDS)
    elif hasattr(future, "done") and future.done():
        future.result()


class SafeConsole:
    """Wraps a Rich Console for thread-safe output during PTK sessions.

    Delegates all output methods through PTK's terminal-yield API when
    an app reference is available. Otherwise falls back to direct calls.
    """

    def __init__(self, console):
        """Initialize with a Rich Console instance.

        Args:
            console: A rich.console.Console instance to wrap.
        """
        self._console = console
        self._app_ref = None  # Set externally: prompt_toolkit Application
        self._lock = threading.Lock()

    def set_app(self, app):
        """Set the current PTK application reference.

        Args:
            app: prompt_toolkit Application instance, or None to clear.
        """
        with self._lock:
            self._app_ref = app

    @property
    def file(self):
        """Pass-through to console.file for flush access."""
        return self._console.file

    @property
    def width(self):
        """Pass-through to console.width."""
        return self._console.width

    def _get_app(self):
        with self._lock:
            return self._app_ref

    async def _run_in_terminal(self, app, func, *args, **kwargs):
        """Erase the live prompt, print above it, then repaint the toolbar."""
        async with in_terminal(render_cli_done=False):
            try:
                return func(*args, **kwargs)
            finally:
                try:
                    self._console.file.flush()
                finally:
                    app.invalidate()

    def _run(self, func, *args, **kwargs):
        """Run a function safely through PTK's terminal-yield machinery."""
        app = self._get_app()
        if app and app.is_running:
            try:
                loop = getattr(app, "loop", None)
                if loop and loop.is_running():
                    try:
                        running_loop = asyncio.get_running_loop()
                    except RuntimeError:
                        running_loop = None

                    if running_loop is loop:
                        future = run_in_terminal(lambda: func(*args, **kwargs))
                        future.add_done_callback(lambda _: app.invalidate())
                    else:
                        future = asyncio.run_coroutine_threadsafe(
                            self._run_in_terminal(app, func, *args, **kwargs),
                            loop,
                        )
                        future.result(timeout=_TERMINAL_PRINT_TIMEOUT_SECONDS)
                else:
                    future = run_in_terminal(lambda: func(*args, **kwargs))
                    if hasattr(future, "result"):
                        _wait_for_future(future)
                    app.invalidate()
                return
            except concurrent.futures.TimeoutError:
                try:
                    app.invalidate()
                except Exception:
                    pass
                return
            except Exception:
                pass  # Fall through to direct call
        func(*args, **kwargs)
        try:
            self._console.file.flush()
        except Exception:
            pass

    def print(self, *args, **kwargs):
        """Thread-safe console.print()."""
        self._run(self._console.print, *args, **kwargs)

    def print_exception(self, *args, **kwargs):
        """Thread-safe console.print_exception()."""
        self._run(self._console.print_exception, *args, **kwargs)

    def log(self, *args, **kwargs):
        """Thread-safe console.log()."""
        self._run(self._console.log, *args, **kwargs)

    def rule(self, *args, **kwargs):
        """Thread-safe console.rule()."""
        self._run(self._console.rule, *args, **kwargs)

    # Pass-through properties/methods that don't need synchronization
    def __getattr__(self, name):
        """Delegate attribute access to the wrapped console.

        This handles read-only properties and methods that don't
        produce terminal output (e.g. console.is_terminal, console.options).
        """
        return getattr(self._console, name)
