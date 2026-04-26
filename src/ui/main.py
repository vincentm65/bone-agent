"""Main entry point for bone-agent chatbot."""

import os
import sys
import time
import random
import threading
import warnings
import atexit
from pathlib import Path

# Suppress prompt_toolkit RuntimeWarning about unawaited coroutines during cleanup
warnings.filterwarnings("ignore", category=RuntimeWarning)

# Add src directory to Python path so we can import llm, core, utils modules
src_dir = Path(__file__).resolve().parent.parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from rich.console import Console
from rich.markdown import Markdown
from rich.theme import Theme
from rich.text import Text
from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.styles import Style

from llm import config
from llm.config import TOOLS_ENABLED
from core.chat_manager import ChatManager
from ui.commands import process_command
from ui.banner import display_startup_banner
from ui.prompt_utils import get_bottom_toolbar_text, setup_common_bindings, TOOLBAR_STYLE
from core.agentic import agentic_answer
from utils.settings import MonokaiDarkBGStyle, left_align_headings
from utils.paths import RG_EXE_PATH
from utils.image_clipboard import read_clipboard_image
from utils.multimodal import ImageAttachment, build_message_content
from exceptions import BoneAgentError

# Console setup
console = Console(theme=Theme({
    "markdown.hr": "grey50",
    "markdown.heading": "default",
    "markdown.h1": "default",
    "markdown.h2": "default",
    "markdown.h3": "default",
    "markdown.h4": "default",
    "markdown.h5": "default",
    "markdown.h6": "default",
    "markdown.paragraph_text": "default",
    "markdown.text": "default",
    "markdown.item": "default",
    "markdown.list_item": "default",
    "markdown.code": "default",
    "markdown.code_block": "default",
    "markdown.link": "default",
    "markdown.link_url": "default",
}))

# Debug mode container (used as mutable reference)
DEBUG_MODE_CONTAINER = {'debug': False}

# Ctrl+C exit tracking (for double Ctrl+C to exit)
CTRL_C_TRACKER = {
    'last_time': 0,
    'exit_window': 2.0,  # 2 second window for double Ctrl+C
    'exit_requested': False
}

# Block input during thinking/agentic processing (prevents key presses from being queued)
INPUT_BLOCKED = {'blocked': False}


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


def check_double_ctrl_c() -> bool:
    """
    Check if this is a double Ctrl+C (within exit window).
    Returns True if should exit, False otherwise.
    Updates the tracker timestamp and exit_requested flag.
    """
    # Check if exit was already requested
    if CTRL_C_TRACKER['exit_requested']:
        return True

    current_time = time.time()
    time_since_last = current_time - CTRL_C_TRACKER['last_time']

    if time_since_last <= CTRL_C_TRACKER['exit_window']:
        # Double Ctrl+C detected - set exit flag and return True
        CTRL_C_TRACKER['exit_requested'] = True
        return True
    else:
        # First Ctrl+C or too much time passed - update timestamp and continue
        CTRL_C_TRACKER['last_time'] = current_time
        return False


def _drain_stdin(session):
    """Drain buffered keystrokes and clear the prompt_toolkit buffer.

    Called after AI processing ends to discard any input the user
    typed while the thinking indicator was active.
    """
    try:
        if os.name != 'nt':
            import termios
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        else:
            import msvcrt
            while msvcrt.kbhit():
                msvcrt.getch()
    except Exception:
        pass

    try:
        buf = session.default_buffer
        if buf and buf.text:
            buf.text = ""
    except Exception:
        pass


def main():
    """Main interactive chat loop."""

    # Load all tools (built-in and user tools)
    # Check for config.yaml — run setup wizard on first run
    from ui.setup_wizard import is_first_run, run_wizard as _run_setup_wizard

    if is_first_run():
        console.print("\n[#5F9EA0]No config found — launching setup wizard.[/#5F9EA0]\n")
        _run_setup_wizard(console)
        # Reload config after wizard writes it
        try:
            from llm import config as llm_config
            llm_config.reload_config()
        except Exception:
            pass
    
    chat_manager = ChatManager()
    thinking_indicator = ThinkingIndicator(console)
    # Safety net: ensure terminal mode is restored even on unhandled exceptions
    def _safety_restore():
        ThinkingIndicator._restore_terminal_mode(thinking_indicator._saved_termios)
    atexit.register(_safety_restore)
    # Start server if needed
    console.print("[yellow]Initializing...[/yellow]")
    chat_manager.server_process = chat_manager.start_server_if_needed()
    if not chat_manager.server_process and chat_manager.client.provider == "local":
        console.print("[red]Failed to start local server![/red]")
        return

    display_startup_banner(chat_manager.approve_mode, clear_screen=True)

    # Start cron scheduler (background thread for scheduled jobs)
    cron_scheduler = None
    try:
        from core.cron import CronScheduler
        cron_scheduler = CronScheduler(console=console)
        cron_scheduler.start()
    except Exception as e:
        import logging as _log
        _log.warning("Cron scheduler failed to start: %s", e)
        console.print(f"[yellow]Cron scheduler unavailable: {e}[/yellow]")

    # First-run onboarding: check if active provider needs an API key but has none
    try:
        from llm import config as llm_config
        active_provider = chat_manager.client.provider
        provider_cfg = llm_config.get_provider_config(active_provider)
        if (
            provider_cfg.get("type") == "api"
            and not provider_cfg.get("api_key")
        ):
            console.print()
            console.print("[bold #5F9EA0]Welcome! Get started in two steps:[/bold #5F9EA0]")
            console.print()
            console.print("  [bold]1.[/bold] [bold white on grey23] /signup <email> [/bold white on grey23]  [dim]— create a free account & API key[/dim]")
            console.print("  [bold]2.[/bold] [bold white on grey23] /provider[/bold white on grey23]          [dim]— or pick another provider (OpenAI, Anthropic, ...)[/dim]")
            console.print()
            console.print("[dim]Tip: use [bold #5F9EA0]/key <your-key>[/bold #5F9EA0] to set a key for any provider.[/dim]")
            console.print()
    except Exception:
        pass  # Best-effort; don't block startup on failure

    # Setup prompt_toolkit with Tab key binding
    bindings = setup_common_bindings(chat_manager)
    pending_attachments = []

    def get_prompt(chat_manager):
        """Return colored prompt."""
        prompt_text = Text.assemble(
            (" > ", "white")
        )         
        with console.capture() as capture:
            console.print(prompt_text, end="")
        return ANSI(capture.get())

    @bindings.add('escape', 'escape')
    def clear_input(event):
        """Clear the current input line on double ESC press (blocked during thinking)."""
        if INPUT_BLOCKED.get('blocked', False):
            return
        buffer = event.app.current_buffer
        if buffer is not None:
            buffer.text = ""
        pending_attachments.clear()
        event.app.invalidate()

    @bindings.add('c-v')
    def paste_image_or_text(event):
        """Paste a clipboard image as an attachment, otherwise fall back to text paste."""
        if INPUT_BLOCKED.get('blocked', False):
            return

        result = read_clipboard_image()
        if result.image:
            attachment = ImageAttachment(
                index=len(pending_attachments) + 1,
                data=result.image.data,
                mime_type=result.image.mime_type,
            )
            pending_attachments.append(attachment)
            event.app.current_buffer.insert_text(attachment.placeholder)
            console.print(f"[dim]Attached {attachment.placeholder} ({attachment.mime_type}).[/dim]")
            event.app.invalidate()
            return

        if result.reason in {"missing_tool", "clipboard_error", "too_large", "unsupported_platform"} and result.message:
            console.print(f"[yellow]{result.message}[/yellow]")
            event.app.invalidate()
            return

        event.app.current_buffer.paste_clipboard_data(event.app.clipboard.get_data())

    session = PromptSession(key_bindings=bindings, style=TOOLBAR_STYLE)

    try:
        while True:
            # Check if exit was requested via double Ctrl+C
            if CTRL_C_TRACKER['exit_requested']:
                break

            try:
                # Use prompt_toolkit for input with Tab key binding and dynamic prompt
                prompt_kwargs = {
                    "bottom_toolbar": lambda: get_bottom_toolbar_text(chat_manager),
                }
                raw_input = session.prompt(
                    lambda: get_prompt(chat_manager),
                    **prompt_kwargs,
                )
                prompt_attachments = list(pending_attachments)
                pending_attachments.clear()
                user_input = raw_input.strip()

                if not user_input and not prompt_attachments:
                    # Clear the empty input line to avoid multiple prompts stacking
                    import sys
                    sys.stdout.write("\033[F\033[K")  # Move up and clear line
                    sys.stdout.flush()
                    continue

                # Process commands
                cmd_result, modified_input = process_command(chat_manager, user_input, console, DEBUG_MODE_CONTAINER, cron_scheduler)
                if cmd_result == "exit":
                    break
                elif cmd_result == "handled":
                    if prompt_attachments:
                        console.print("[dim]Note: Pasted attachments discarded by slash command.[/dim]")
                    continue

                # Use modified input if provided (from /edit command)
                final_input = modified_input if modified_input else user_input
                final_content = build_message_content(final_input, prompt_attachments)

                chat_manager.maybe_auto_compact(console)

                thinking_indicator.start()
                INPUT_BLOCKED['blocked'] = True
                try:
                    console.print("─" * console.width, style="rgb(30,30,30)")
                    console.print()  # Extra newline after user input to separate from LLM response
                    # Add user message
                    if TOOLS_ENABLED:
                        try:
                            agentic_answer(
                                chat_manager,
                                final_content,
                                console,
                                Path.cwd().resolve(),
                                RG_EXE_PATH,
                                DEBUG_MODE_CONTAINER['debug'],
                                thinking_indicator=thinking_indicator,
                            )
                            chat_manager._update_context_tokens()
                        except KeyboardInterrupt:
                            if not check_double_ctrl_c():
                                console.print("\n[yellow]Response interrupted (Ctrl+C). Press Ctrl+C again to exit.[/yellow]")
                            console.print()  # Extra spacing
                        except BoneAgentError as e:
                            # Handle all bone-agent custom exceptions gracefully
                            console.print(f"[red]Error: {e}[/red]", markup=False)
                            if hasattr(e, 'details') and e.details:
                                console.print(f"[dim]Details: {e.details}[/dim]", markup=False)
                    else:
                        chat_manager.messages.append({"role": "user", "content": final_content})
                        chat_manager.log_message({"role": "user", "content": final_content})

                        try:
                            stream = chat_manager.client.chat_completion(
                                chat_manager.messages, stream=True
                            )
                            if isinstance(stream, str):
                                console.print(f"[red]Error: {stream}[/red]")
                                continue

                            try:
                                # Stream response
                                chunks = []
                                usage_data = None
                                for chunk in stream:
                                    # Check if this is usage data (final chunk)
                                    if isinstance(chunk, dict) and '__usage__' in chunk:
                                        usage_data = chunk['__usage__']
                                    else:
                                        chunks.append(chunk)
                                full_response = "".join(chunks)

                                # Clear thinking indicator before printing response
                                thinking_indicator.stop(reset=True)
                                INPUT_BLOCKED['blocked'] = False
                                _drain_stdin(session)

                                if full_response.strip():
                                    md = Markdown(left_align_headings(full_response), code_theme=MonokaiDarkBGStyle, justify="left")
                                    console.print(md)

                                chat_manager.messages.append(
                                    {"role": "assistant", "content": full_response}
                                )

                                # Add usage tracking (resolves cost from config if
                                # upstream-reported cost is absent in the usage dict)
                                if usage_data:
                                    provider_cfg = llm.config.get_provider_config(chat_manager.client.provider)
                                    chat_manager.token_tracker.add_usage(
                                        usage_data,
                                        model_name=provider_cfg.get("model", ""),
                                    )

                                chat_manager._update_context_tokens()
                            except KeyboardInterrupt:
                                # Ctrl+C pressed during streaming
                                if not check_double_ctrl_c():
                                    console.print("\n[yellow]Response interrupted (Ctrl+C). Press Ctrl+C again to exit.[/yellow]")
                                    # Save partial response
                                    if chunks:
                                        partial = "".join(chunks)
                                        if partial.strip():
                                            partial_with_note = partial + "\n\n*[Response interrupted]*"
                                            md = Markdown(left_align_headings(partial_with_note), code_theme=MonokaiDarkBGStyle, justify="left")
                                            console.print(md)
                                            chat_manager.messages.append(
                                                {"role": "assistant", "content": partial}
                                            )
                                console.print()  # Extra spacing
                            finally:
                                # Ensure HTTP connection is closed
                                if hasattr(stream, 'close'):
                                    stream.close()

                        except BoneAgentError as e:
                            # Handle all bone-agent custom exceptions gracefully
                            console.print(f"[red]Error: {e}[/red]", markup=False)
                            if hasattr(e, 'details') and e.details:
                                console.print(f"[dim]Details: {e.details}[/dim]", markup=False)
                        except Exception as e:
                            console.print(f"[red]Error during generation: {e}[/red]", markup=False)
                finally:
                    thinking_indicator.stop(reset=True)
                    INPUT_BLOCKED['blocked'] = False
                    _drain_stdin(session)

            except KeyboardInterrupt:
                # Ctrl+C pressed while waiting for input
                if check_double_ctrl_c():
                    break
                else:
                    console.print("\n[dim](Press Ctrl+C again to exit, or type 'exit' to quit)[/dim]")
                    continue
            except EOFError:
                # stdin closed (Ctrl+D or piped input ended)
                break

    finally:
        # Display session summary before cleanup
        summary = chat_manager.token_tracker.get_session_summary()
        console.print(f"\n[white]Session Summary: {summary}[/white]")

        # Stop cron scheduler if running
        if cron_scheduler:
            cron_scheduler.stop()

        chat_manager.cleanup()
        console.print("[yellow]Goodbye![/yellow]")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="bone-agent CLI")
    parser.add_argument("--cron-run", metavar="JOB_ID", help="Run a cron job headlessly and exit")
    args = parser.parse_args()

    if args.cron_run:
        from core.cron import run_job_headless
        sys.exit(run_job_headless(args.cron_run))

    main()
