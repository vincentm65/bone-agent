"""Main entry point for vmCode chatbot."""

import os
import sys
import time
from pathlib import Path

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
        self._status = None
        self._active = False

    def start(self):
        if self._status is None:
            self._status = self.console.status(self.message, spinner=self.spinner, spinner_style="cyan")
        if not self._active:
            self._status.start()
            self._active = True

    def stop(self):
        if self._status and self._active:
            self._status.stop()
            self._active = False

    def pause(self):
        self.stop()

    def resume(self):
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
            console.print(f"[dim]You can also set keys interactively with: /key <your-key>[/dim]\n")
        else:
            console.print("[red]config.yaml.example not found. Please reinstall vmcode.[/red]\n")
        
        # Continue anyway - user can set keys via /key command
        console.print("[yellow]Continuing... You can set API keys with the /key command.[/yellow]\n")
    
    chat_manager = ChatManager()
    thinking_indicator = ThinkingIndicator(console)
    # Start server if needed
    console.print("[yellow]Initializing...[/yellow]")
    chat_manager.server_process = chat_manager.start_server_if_needed()
    if not chat_manager.server_process and chat_manager.client.provider == "local":
        console.print("[red]Failed to start local server![/red]")
        return

    display_startup_banner(chat_manager.approve_mode, chat_manager.interaction_mode)

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
        """Toggle between Plan and Edit modes."""
        chat_manager.toggle_interaction_mode()
        event.app.invalidate()

    @bindings.add('escape', 'escape')
    def clear_input(event):
        """Clear the current input line on double ESC press."""
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
                user_input = session.prompt(
                    lambda: get_prompt(chat_manager),
                    **prompt_kwargs,
                ).strip()

                if not user_input:
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
                    thinking_indicator.stop()

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
