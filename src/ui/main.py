"""Main entry point for vmCode chatbot."""

import os
import sys
import time
import random
import threading
import warnings
from pathlib import Path

# Suppress prompt_toolkit RuntimeWarning about unawaited coroutines during cleanup
warnings.filterwarnings("ignore", category=RuntimeWarning)

# Add src directory to Python path so we can import llm, core, utils modules
src_dir = Path(__file__).resolve().parent.parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from rich.console import Console
from rich.theme import Theme
from rich.markdown import Markdown
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
from utils.settings import MonokaiDarkBGStyle
from utils.markdown import left_align_headings
from exceptions import VmCodeError
from tools.loader import load_all_tools

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

# Path constants
REPO_ROOT = Path.cwd().resolve()
APP_ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parents[2]
)
# Platform-agnostic ripgrep path: 'rg' on Unix/Linux, 'rg.exe' on Windows
RG_EXE_NAME = "rg.exe" if os.name == "nt" else "rg"
RG_EXE_PATH = (APP_ROOT / "bin" / RG_EXE_NAME).resolve()


class ThinkingIndicator:
    """Simple spinner wrapper that always cleans up."""

    def __init__(self, console, message="Thinking ...", spinner="dots"):
        self.console = console
        self.message = message
        self.spinner = spinner
        self._last_word_change = 0
        self._word_change_interval = 15.0  # Change word every 5 seconds
        
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
            "Scanning fingerpints ...",
            "Rerouting ...",
            "Refactoring ...",
            "Burning tokens ...",
            "Conjuring ...",
            "Recalculating ...",
            "Spinning ...",
            "Pointing ...",
            "Demateralizing ...",
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
            "\"Engineering\" ...",
            "Deleting (jk) ...",
            "Computer... Fix my program",
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
            "Reading the docs ...",
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
            "Pondeing the orb ...",
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
        self._status = self.console.status(self.message, spinner=self.spinner, spinner_style="cyan")
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

            # Change word every 5 seconds
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

    def stop(self, show_completion=False):
        # Calculate and store elapsed time (including accumulated pauses)
        elapsed_time = None
        if self._start_time:
            elapsed_time = self._elapsed_before_pause + (time.time() - self._start_time)
            self._elapsed_before_pause = elapsed_time
        
        # Stop timer thread
        self._stop_timer.set()
        if self._timer_thread:
            self._timer_thread.join(timeout=0.5)
        
        if self._status and self._active:
            self._status.stop()
            self._active = False
        
        # Display final elapsed time only on true completion
        if show_completion and elapsed_time is not None:
            # Reset state after true completion
            self._has_been_started = False
            self._elapsed_before_pause = 0.0
        
        self._start_time = None

    def pause(self):
        # Stop without showing completion time (accumulates elapsed time)
        self.stop(show_completion=False)
        # Ensure status line cleanup is fully flushed to terminal
        try:
            self.console.file.flush()
        except Exception:
            pass

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
    # This populates the ToolRegistry with all decorated tools
    load_all_tools()

    # Check for config.yaml and provide helpful message if missing
    config_path = Path(__file__).resolve().parents[1].parent / "config.yaml"
    config_example = Path(__file__).resolve().parents[1].parent / "config.yaml.example"
    
    if not config_path.exists():
        console.print("\n[yellow]No config.yaml found![/yellow]")
        console.print("\n[cyan]Getting Started:[/cyan]\n")
        
        if config_example.exists():
            console.print(f"1. Copy the example config:")
            console.print(f"   [dim]cp config.yaml.example config.yaml[/dim]\n")
            console.print(f"2. Edit config.yaml and add your API keys\n")
            console.print(f"3. Or set environment variables:")
            console.print(f"   [dim]export OPENAI_API_KEY='sk-your-key'[/dim]\n")
            console.print(f"4. Then run: [green]vmcode[/green]\n")
            console.print(f"[dim]You can also set keys interactively with: [bold cyan]/key[/bold cyan] <your-key>[/dim]\n")
        else:
            console.print("[red]config.yaml.example not found. Please reinstall vmcode.[/red]\n")
        
        # Continue anyway - user can set keys via /key command
        console.print("[yellow]Continuing... You can set API keys with the [bold cyan]/key[/bold cyan] command.[/yellow]\n")
    
    chat_manager = ChatManager()
    thinking_indicator = ThinkingIndicator(console)
    # Start server if needed
    console.print("[yellow]Initializing...[/yellow]")
    chat_manager.server_process = chat_manager.start_server_if_needed()
    if not chat_manager.server_process and chat_manager.client.provider == "local":
        console.print("[red]Failed to start local server![/red]")
        return

    display_startup_banner(chat_manager.approve_mode, chat_manager.interaction_mode)

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
            console.print("[bold cyan]Welcome! Get started in two steps:[/bold cyan]")
            console.print()
            console.print("  [bold]1.[/bold] [bold white on grey23] /signup <email> [/bold white on grey23]  [dim]— create a free account & API key[/dim]")
            console.print("  [bold]2.[/bold] [bold white on grey23] /provider[/bold white on grey23]          [dim]— or pick another provider (OpenAI, Anthropic, ...)[/dim]")
            console.print()
            console.print("[dim]Tip: use [bold cyan]/key <your-key>[/bold cyan] to set a key for any provider.[/dim]")
            console.print()
    except Exception:
        pass  # Best-effort; don't block startup on failure

    # Setup prompt_toolkit with Tab key binding
    bindings = setup_common_bindings(chat_manager)

    def get_prompt(chat_manager):
        """Return colored prompt based on current mode."""
        if chat_manager.interaction_mode == "plan":
            prompt_text = Text.assemble(
                (" Plan", "bold cyan"),
                (" > ", "white")
            )
        elif chat_manager.interaction_mode == "edit":
            prompt_text = Text.assemble(
                (" Edit", "green"),
                (" > ", "white")
            )
        else:
            prompt_text = Text.assemble(
                (" Learn", "magenta"),
                (" > ", "white")
            )         
        with console.capture() as capture:
            console.print(prompt_text, end="")
        return ANSI(capture.get())

    @bindings.add('tab')
    def toggle_mode(event):
        """Toggle between Plan and Edit modes (blocked during thinking)."""
        if INPUT_BLOCKED.get('blocked', False):
            return
        chat_manager.toggle_interaction_mode()
        event.app.invalidate()

    @bindings.add('escape', 'escape')
    def clear_input(event):
        """Clear the current input line on double ESC press (blocked during thinking)."""
        if INPUT_BLOCKED.get('blocked', False):
            return
        buffer = event.app.current_buffer
        if buffer is not None:
            buffer.text = ""
        event.app.invalidate()

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
                user_input = raw_input.strip()

                if not user_input:
                    # Clear the empty input line to avoid multiple prompts stacking
                    import sys
                    sys.stdout.write("\033[F\033[K")  # Move up and clear line
                    sys.stdout.flush()
                    continue

                # Process commands
                cmd_result, modified_input = process_command(chat_manager, user_input, console, DEBUG_MODE_CONTAINER)
                if cmd_result == "exit":
                    break
                elif cmd_result == "handled":
                    continue

                # Use modified input if provided (from /edit command)
                final_input = modified_input if modified_input else user_input

                chat_manager.maybe_auto_compact(console)

                thinking_indicator.start()
                INPUT_BLOCKED['blocked'] = True
                try:
                    console.print()  # Extra newline after user input to separate from LLM response
                    # Add user message
                    if TOOLS_ENABLED:
                        chat_manager.command_history.clear()
                        try:
                            agentic_answer(
                                chat_manager,
                                final_input,
                                console,
                                REPO_ROOT,
                                RG_EXE_PATH,
                                DEBUG_MODE_CONTAINER['debug'],
                                thinking_indicator=thinking_indicator,
                            )
                            chat_manager._update_context_tokens()
                        except KeyboardInterrupt:
                            if not check_double_ctrl_c():
                                console.print("\n[yellow]Response interrupted (Ctrl+C). Press Ctrl+C again to exit.[/yellow]")
                            console.print()  # Extra spacing
                        except VmCodeError as e:
                            # Handle all vmCode custom exceptions gracefully
                            console.print(f"[red]Error: {e}[/red]", markup=False)
                            if hasattr(e, 'details') and e.details:
                                console.print(f"[dim]Details: {e.details}[/dim]", markup=False)
                    else:
                        chat_manager.messages.append({"role": "user", "content": final_input})

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

                                if full_response.strip():
                                    md = Markdown(left_align_headings(full_response), code_theme=MonokaiDarkBGStyle, justify="left")
                                    console.print(md)

                                chat_manager.messages.append(
                                    {"role": "assistant", "content": full_response}
                                )

                                # Add usage tracking
                                if usage_data:
                                    chat_manager.token_tracker.add_usage(usage_data)

                                chat_manager._update_context_tokens()

                                console.print()  # Extra spacing
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

                        except VmCodeError as e:
                            # Handle all vmCode custom exceptions gracefully
                            console.print(f"[red]Error: {e}[/red]", markup=False)
                            if hasattr(e, 'details') and e.details:
                                console.print(f"[dim]Details: {e.details}[/dim]", markup=False)
                        except Exception as e:
                            console.print(f"[red]Error during generation: {e}[/red]", markup=False)
                finally:
                    thinking_indicator.stop(show_completion=True)
                    INPUT_BLOCKED['blocked'] = False
                    _drain_stdin(session)

            except KeyboardInterrupt:
                # Ctrl+C pressed while waiting for input
                if check_double_ctrl_c():
                    break
                else:
                    console.print("\n[dim](Press Ctrl+C again to exit, or type 'exit' to quit)[/dim]")
                    continue

    finally:
        # Display session summary before cleanup
        summary = chat_manager.token_tracker.get_session_summary()
        console.print(f"\n[white]Session Summary: {summary}[/white]")

        chat_manager.cleanup()
        console.print("[yellow]Goodbye![/yellow]")


if __name__ == "__main__":
    main()
