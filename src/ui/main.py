"""Main entry point for bone-agent chatbot."""

import os
import shlex
import sys
import time
import random
import threading
import asyncio
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
from rich.panel import Panel
from rich.theme import Theme
from rich.text import Text
from prompt_toolkit import PromptSession
from prompt_toolkit.application import in_terminal, run_in_terminal
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.styles import Style

from ui.safe_console import SafeConsole

from llm import config
from llm.config import TOOLS_ENABLED
from core.chat_manager import ChatManager
from ui.commands import process_command
from ui.banner import display_startup_banner
from ui.prompt_utils import get_bottom_toolbar_text, setup_common_bindings, TOOLBAR_STYLE
from ui.toolbar_interactions import (
    set_active_interaction,
    clear_active_interaction,
    get_active_interaction,
    patch_for_active_prompt,
    SETTING_RESOLVED_SENTINEL,
    BOUNDARY_RESOLVED_SENTINEL,
    COMMAND_CONFIRM_SENTINEL,
)
from core.agentic import agentic_answer
from utils.settings import MonokaiDarkBGStyle, left_align_headings, swarm_settings
from utils.paths import RG_EXE_PATH
from utils.image_clipboard import read_clipboard_image, read_image_file
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

from ui.thinking import ThinkingIndicator

# Block input during thinking/agentic processing (prevents key presses from being queued)
INPUT_BLOCKED = {'blocked': False}

# Timer for advancing toolbar spinner frames during agent work
_spinner_timer = None  # threading.Timer instance
SPINNER_REFRESH_INTERVAL = 0.1


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


from core.swarm_auto_turn import drain_inbox_to_prompts


# ── Helper: toolbar spinner management ────────────────────────────

def _start_progress_spinner(chat_manager, message=""):
    """Start toolbar spinner and frame-advance timer."""
    chat_manager.progress.start_spinner(message)
    _schedule_spinner_advance(chat_manager)


def _schedule_spinner_advance(chat_manager):
    """Schedule next spinner frame advance."""
    global _spinner_timer
    if chat_manager.progress.spinner_active or chat_manager.progress.subagent_active:
        chat_manager.progress.advance_spinner()
        chat_manager.invalidate_toolbar()
        _spinner_timer = threading.Timer(SPINNER_REFRESH_INTERVAL, _schedule_spinner_advance, args=[chat_manager])
        _spinner_timer.daemon = True
        _spinner_timer.start()


def _stop_progress_spinner(chat_manager):
    """Stop toolbar spinner and cancel timer."""
    global _spinner_timer
    if _spinner_timer:
        _spinner_timer.cancel()
        _spinner_timer = None
    chat_manager.progress.stop_spinner()
    chat_manager.invalidate_toolbar()


async def _safe_print_in_terminal(app, console, *args, **kwargs):
    """Erase the live prompt, print above it, then repaint the toolbar."""
    async with in_terminal(render_cli_done=False):
        try:
            console.print(*args, **kwargs)
        finally:
            console.file.flush()
            app.invalidate()


def _safe_print(console, session, *args, **kwargs):
    """Print through PTK's terminal-yield API to avoid toolbar artifacts."""
    app = getattr(session, 'app', None)
    if app and app.is_running:
        loop = getattr(app, "loop", None)
        if loop and loop.is_running():
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None

            if running_loop is loop:
                future = run_in_terminal(lambda: console.print(*args, **kwargs))
                future.add_done_callback(lambda _: app.invalidate())
            else:
                future = asyncio.run_coroutine_threadsafe(
                    _safe_print_in_terminal(app, console, *args, **kwargs),
                    loop,
                )
                future.result()
        else:
            future = run_in_terminal(lambda: console.print(*args, **kwargs))
            if hasattr(future, "result"):
                future.result()
            app.invalidate()
    else:
        console.print(*args, **kwargs)


# ── Helper: agent-in-thread wrapper ───────────────────────────────

def _run_agent_in_thread(chat_manager, user_content, console, safe_console,
                         cwd, rg_path, debug, completion_event, result_holder):
    """Run agentic_answer in a background thread.

    All Rich output goes through *safe_console* (which routes via PTK's
    ``run_in_terminal()``), keeping PTK alive for toolbar rendering.
    """
    try:
        chat_manager.clear_agent_cancel()
        agentic_answer(
            chat_manager, user_content, safe_console,
            cwd, rg_path, debug,
            thinking_indicator=None,  # No Rich spinner — toolbar handles it
        )
        chat_manager._update_context_tokens()
    except KeyboardInterrupt:
        safe_console.print("\n[yellow]Response interrupted.[/yellow]")
    except BoneAgentError as e:
        safe_console.print(f"Error: {e}", style="red", markup=False)
        if hasattr(e, 'details') and e.details:
            safe_console.print(f"Details: {e.details}", style="dim", markup=False)
    except Exception as e:
        safe_console.print(f"[red]Error during agent work: {e}[/red]", markup=False)
    finally:
        result_holder['done'] = True
        completion_event.set()


def _run_resume_in_thread(chat_manager, resume_method_name, completion_event, result_holder):
    """Resume a suspended orchestrator while the PTK prompt stays alive."""
    try:
        orchestrator = getattr(chat_manager, '_agentic_orchestrator', None)
        if orchestrator is None:
            result_holder['result'] = "done"
            return
        resume_method = getattr(orchestrator, resume_method_name)
        result_holder['result'] = resume_method(None)
        try:
            chat_manager._update_context_tokens()
        except Exception:
            pass
    except KeyboardInterrupt:
        result_holder['interrupted'] = True
    except BoneAgentError as e:
        result_holder['error'] = e
        try:
            console = getattr(getattr(chat_manager, '_agentic_orchestrator', None), 'console', None)
            if console:
                console.print(f"Error: {e}", style="red", markup=False)
                if hasattr(e, 'details') and e.details:
                    console.print(f"Details: {e.details}", style="dim", markup=False)
        except Exception:
            pass
    except Exception as e:
        result_holder['error'] = e
        try:
            console = getattr(getattr(chat_manager, '_agentic_orchestrator', None), 'console', None)
            if console:
                console.print(f"[red]Error during agent work: {e}[/red]", markup=False)
        except Exception:
            pass
    finally:
        result_holder['done'] = True
        completion_event.set()


def _resume_orchestrator_with_live_toolbar(
    chat_manager,
    session,
    safe_console,
    resume_method_name,
):
    """Run a suspended-agent resume with the status toolbar still visible."""
    completion_event = threading.Event()
    result_holder = {'done': False}
    safe_console.set_app(session.app)
    _start_progress_spinner(chat_manager, "Thinking ...")
    agent_thread = threading.Thread(
        target=_run_resume_in_thread,
        args=(chat_manager, resume_method_name, completion_event, result_holder),
        daemon=True,
    )
    agent_thread.start()
    raw_input = session.prompt(
        lambda: "",
        bottom_toolbar=lambda: get_bottom_toolbar_text(chat_manager),
        inputhook=_create_agent_done_inputhook(
            completion_event,
            on_complete=lambda: _stop_progress_spinner(chat_manager),
        ),
    )
    _stop_progress_spinner(chat_manager)
    safe_console.set_app(None)
    agent_thread.join(timeout=5.0)
    return raw_input, result_holder


# ── Helper: inputhook for agent-done waiting ──────────────────────

def _create_agent_done_inputhook(completion_event, on_complete=None):
    """Inputhook that exits the prompt when agent work completes.

    Polls *completion_event* every 50ms.  When set, calls *on_complete*
    (if provided) **before** exiting the prompt, then exits with sentinel
    value ``999`` which the main loop handles to continue.

    The *on_complete* callable (e.g. ``_stop_progress_spinner``) runs while
    the PTK app is still alive so toolbar invalidation can repaint and
    clear the spinner line.
    """
    def inputhook(context):
        while True:
            if completion_event.is_set():
                if on_complete is not None:
                    try:
                        on_complete()
                    except Exception:
                        pass
                from prompt_toolkit.application import get_app
                get_app().exit(result=999)
                return
            if context.input_is_ready():
                return
            time.sleep(0.05)
    return inputhook


# ── Helpers: background-prompt cleanup ────────────────────────────

def _cleanup_bg_prompt(chat_manager, session, safe_console, agent_thread=None):
    """Standard cleanup after a background-thread prompt completes normally.

    Stops the toolbar spinner, detaches the PTK app from *safe_console*,
    unblocks input, drains buffered keystrokes, and optionally joins the
    agent thread.
    """
    _stop_progress_spinner(chat_manager)
    safe_console.set_app(None)
    INPUT_BLOCKED['blocked'] = False
    _drain_stdin(session)
    if agent_thread is not None:
        agent_thread.join(timeout=5.0)


def _handle_ctrl_c_bg_prompt(chat_manager, session, safe_console,
                              agent_thread, completion_event,
                              cancel_msg="Subagent cancelled.",
                              idle_msg="Cancelled (Ctrl+C). Press Ctrl+C again to exit."):
    """Handle KeyboardInterrupt during a background-thread prompt.

    Two branches:

    1. **Active subagent** — signal cancellation and join the thread.
       The worker/subagent panel owns final state and the command-specific
       cancel message; this helper only stops the spinner and invalidates.
    2. **No subagent** — basic cleanup, then double-Ctrl-C check and
       print *idle_msg*.

    In both cases the caller should ``continue`` the main loop afterward.
    """
    chat_manager.request_agent_cancel()
    if chat_manager.progress.get_subagent_summary()["active"]:
        # Signal cancellation — the worker/subagent owns final panel state
        # and prints the command-specific cancel message (e.g. "Ask cancelled.").
        chat_manager.request_subagent_cancel()
        chat_manager.progress.clear_subagent()
        chat_manager.progress.clear_active_tool()
        chat_manager.progress.stop_spinner()
        chat_manager.invalidate_toolbar()
        _cleanup_bg_prompt(chat_manager, session, safe_console, agent_thread)
        completion_event.set()
    else:
        _cleanup_bg_prompt(chat_manager, session, safe_console, agent_thread)
        if not check_double_ctrl_c():
            console.print(f"\n[yellow]{idle_msg}[/yellow]")


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
    thinking_indicator = ThinkingIndicator(console, chat_manager=chat_manager)
    safe_console = SafeConsole(console)
    # Stop swarm server on process exit (best-effort cleanup)
    def _stop_swarm_server():
        try:
            if chat_manager.swarm_admin_mode and chat_manager.swarm_server:
                chat_manager.swarm_server.stop()
                chat_manager.swarm_admin_mode = False
                chat_manager.swarm_server = None
        except Exception:
            pass
    atexit.register(_stop_swarm_server)
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

    # Start background swarm inbox poller (daemon thread).
    # Drains server inbox items into a queue that the agentic loop checks
    # mid-turn, so swarm events are processed even during long LLM calls.

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

    def paste_text_from_clipboard(event):
        """Fall back to prompt_toolkit's normal text paste behavior."""
        event.app.current_buffer.paste_clipboard_data(event.app.clipboard.get_data())

    def attach_image(image, *, insert_placeholder=None, source=None):
        """Attach an image to the current prompt."""
        attachment = ImageAttachment(
            index=len(pending_attachments) + 1,
            data=image.data,
            mime_type=image.mime_type,
        )
        pending_attachments.append(attachment)
        if insert_placeholder:
            insert_placeholder(attachment.placeholder)
        source_text = f" from {source}" if source else ""
        console.print(
            f"[dim]Attached {attachment.placeholder}{source_text} ({attachment.mime_type}, {len(attachment.data) // 1024} KB).[/dim]"
        )
        return attachment

    def attach_image_from_path(path_text, *, insert_placeholder=None):
        """Attach an image file by path and report any validation errors."""
        result = read_image_file(path_text)
        if result.image:
            return attach_image(result.image, insert_placeholder=insert_placeholder, source=path_text)
        console.print(f"[yellow]{result.message or 'Could not attach image file.'}[/yellow]")
        return None

    def get_prompt(chat_manager):
        """Return colored prompt."""
        if (
            get_active_interaction(chat_manager) is not None
            or getattr(chat_manager, "_pending_interaction", None) is not None
        ):
            return ANSI("")
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
        if pending_attachments:
            console.print("[dim]Cleared pending image attachments.[/dim]")
        pending_attachments.clear()
        event.app.invalidate()

    @bindings.add('c-s-left')
    def swarm_status_page_previous(event):
        """Swarm status previous page (Ctrl+Shift+Left)."""
        if INPUT_BLOCKED.get('blocked', False):
            return
        if not getattr(chat_manager, 'swarm_admin_mode', False):
            return
        page = getattr(chat_manager, 'swarm_status_page', 0)
        chat_manager.swarm_status_page = max(0, page - 1)
        event.app.invalidate()

    @bindings.add('c-s-right')
    def swarm_status_page_next(event):
        """Swarm status next page (Ctrl+Shift+Right)."""
        if INPUT_BLOCKED.get('blocked', False):
            return
        if not getattr(chat_manager, 'swarm_admin_mode', False):
            return
        page = getattr(chat_manager, 'swarm_status_page', 0)
        chat_manager.swarm_status_page = min(1, page + 1)
        event.app.invalidate()

    @bindings.add('c-v')
    def paste_image_or_text(event):
        """Paste a clipboard image as an attachment, otherwise fall back to text paste."""
        if INPUT_BLOCKED.get('blocked', False):
            return

        result = read_clipboard_image()
        if result.image:
            attach_image(result.image, insert_placeholder=event.app.current_buffer.insert_text)
            event.app.invalidate()
            return

        if result.reason in {"clipboard_error", "too_large"} and result.message:
            console.print(f"[yellow]{result.message}[/yellow]")
            event.app.invalidate()
            return

        if result.reason in {"missing_tool", "unsupported_platform"} and result.message:
            console.print(f"[dim]{result.message} Falling back to text paste.[/dim]")
            event.app.invalidate()

        paste_text_from_clipboard(event)

    def consume_image_attach_lines(text):
        """Attach leading /image lines and return the remaining prompt text."""
        remaining_lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("/image"):
                remaining_lines.append(line)
                continue

            try:
                parts = shlex.split(stripped)
            except ValueError as exc:
                console.print(f"[yellow]Invalid /image command: {exc}[/yellow]")
                continue

            if len(parts) != 2:
                console.print("[yellow]Usage: /image path/to/file.png[/yellow]")
                continue

            attachment = attach_image_from_path(parts[1])
            if attachment:
                remaining_lines.append(attachment.placeholder)

        return "\n".join(remaining_lines).strip()

    session = PromptSession(key_bindings=bindings, style=TOOLBAR_STYLE)

    # Register PTK toolbar invalidation callback (called from background thread)
    chat_manager._invalidate_toolbar = lambda: (
        session.app.invalidate()
        if session.app and session.app.is_running
        else None
    )

    handoff_to_worker = False

    try:
        while True:
            # Check if exit was requested via double Ctrl+C
            if CTRL_C_TRACKER['exit_requested']:
                break

            try:
                # Use prompt_toolkit for input with swarm-aware interrupt.
                # pre_run launches an asyncio background task that calls
                # get_app().exit(130) when swarm work arrives; the inputhook
                # is a belt-and-suspenders fallback for edge cases.
                from ui.prompt_interrupts import create_swarm_inputhook, create_swarm_pre_run

                prompt_kwargs = {
                    "bottom_toolbar": lambda: get_bottom_toolbar_text(chat_manager),
                }

                raw_input = session.prompt(
                    lambda: get_prompt(chat_manager),
                    inputhook=create_swarm_inputhook(chat_manager),
                    pre_run=create_swarm_pre_run(chat_manager),
                    **prompt_kwargs,
                )
                # Sentinel 130 from swarm inputhook — pending work in server inbox.
                # Drain pending approvals, build a synthetic prompt, and fall
                # through to agentic_answer.  Loops via continue if nothing to do.
                #
                # Fallback: if the inputhook failed to interrupt (e.g. race with
                # user pressing Enter), detect pending items directly.  Only
                # trigger when the user hasn't typed a substantive message, so
                # real user input is never intercepted.
                if raw_input != 130 and chat_manager.has_pending_swarm_work():
                    user_text = raw_input.strip() if isinstance(raw_input, str) else ""
                    if not user_text:
                        raw_input = 130

                if raw_input == 130:
                    pending_prompts = []
                    # Drain server inbox via shared helper (also used by
                    # the background poller and agentic mid-turn injection).
                    server = getattr(chat_manager, 'swarm_server', None)
                    if server:
                        pending_prompts.extend(drain_inbox_to_prompts(server))

                    # Also drain the inject queue — items the background
                    # poller already moved out of the server inbox.
                    pending_prompts.extend(chat_manager.drain_inject_queue())

                    if not pending_prompts:
                        continue

                    final_content = "\n\n---\n\n".join(pending_prompts)
                    # Skip user-input parsing, command processing, and attachment
                    # handling — go straight to the agentic_answer call below.
                    chat_manager.maybe_auto_compact(console)

                    # Clear the orphaned prompt line — prompt_toolkit already
                    # painted " > " before the inputhook exited with 130.
                    import sys
                    sys.stdout.write("\033[F\033[K")
                    sys.stdout.flush()

                    INPUT_BLOCKED['blocked'] = True
                    if TOOLS_ENABLED:
                        # Background thread + living PTK for toolbar progress
                        completion_event = threading.Event()
                        result_holder = {'done': False}
                        safe_console.set_app(session.app)
                        _start_progress_spinner(chat_manager, "Thinking ...")
                        console.print("─" * console.width, style="rgb(30,30,30)")
                        console.print()
                        agent_thread = threading.Thread(
                            target=_run_agent_in_thread,
                            args=(chat_manager, final_content, console, safe_console,
                                  Path.cwd().resolve(), RG_EXE_PATH,
                                  DEBUG_MODE_CONTAINER['debug'],
                                  completion_event, result_holder),
                            daemon=True,
                        )
                        agent_thread.start()
                        raw_input = session.prompt(
                            lambda: "",
                            bottom_toolbar=lambda: get_bottom_toolbar_text(chat_manager),
                            inputhook=_create_agent_done_inputhook(
                                completion_event,
                                on_complete=lambda: _stop_progress_spinner(chat_manager),
                            ),
                        )
                        _stop_progress_spinner(chat_manager)  # idempotent fallback
                        safe_console.set_app(None)
                        INPUT_BLOCKED['blocked'] = False
                        _drain_stdin(session)
                        agent_thread.join(timeout=5.0)
                    # Else: non-tools path.  Auto-turns need tools to process
                    # approvals and completions; skip with a warning if disabled.
                    else:
                        chat_manager.messages.append({"role": "user", "content": final_content})
                        chat_manager.log_message({"role": "user", "content": final_content})
                    continue

                # Agent work completed sentinel (999).  Inputhook for the
                # background-thread agent path exits with this value after
                # the work finishes.  Normal flow: the local block in the
                # TOOLS_ENABLED path handles cleanup and continues before
                # reaching here.  This is a safety net for any code path
                # that might leak the sentinel.
                if isinstance(raw_input, int) and raw_input == 999:
                    chat_manager.progress.clear_all()
                    continue

                # Tool approval resolved via toolbar — resume the suspended
                # agentic orchestrator.  The active interaction's result was
                # captured when the user selected accept/advise/cancel, and
                # the interaction's done event was set by ToolApprovalPending.
                # The orchestrator (stashed after agentic_answer returned
                # "suspended") picks it up and continues.
                if raw_input == 131 and getattr(chat_manager, '_agentic_orchestrator', None) is not None:
                    INPUT_BLOCKED['blocked'] = True
                    try:
                        _resume_orchestrator_with_live_toolbar(
                            chat_manager,
                            session,
                            safe_console,
                            "resume_after_approval",
                        )
                    finally:
                        INPUT_BLOCKED['blocked'] = False
                        _drain_stdin(session)
                    continue

                # Select_option resolved via toolbar — resume the suspended
                # agentic orchestrator.  The user made a selection (or
                # cancelled) in the active toolbar interaction, which set the
                # done event and exited the prompt with sentinel 132.
                if raw_input == 132 and getattr(chat_manager, '_agentic_orchestrator', None) is not None:
                    INPUT_BLOCKED['blocked'] = True
                    try:
                        _resume_orchestrator_with_live_toolbar(
                            chat_manager,
                            session,
                            safe_console,
                            "resume_after_selection",
                        )
                    finally:
                        INPUT_BLOCKED['blocked'] = False
                        _drain_stdin(session)
                    continue

                # Setting selector resolved via toolbar — the user finished
                # interacting with a command-driven SettingSelector (e.g.
                # /config, /provider, /tools, /obsidian).  The selector's
                # finish() exited the prompt with sentinel 133.  Call the
                # stored continuation to apply the changes.
                if raw_input == 133 and getattr(chat_manager, '_setting_selector', None) is not None:
                    selector = chat_manager._setting_selector
                    continuation = getattr(chat_manager, '_setting_continuation', None)
                    try:
                        if continuation is not None:
                            continuation(selector)
                    finally:
                        new_selector = getattr(chat_manager, '_setting_selector', None)
                        if new_selector is not selector:
                            # Continuation replaced the selector — patch and activate the new one
                            patch_for_active_prompt(new_selector, SETTING_RESOLVED_SENTINEL)
                            set_active_interaction(chat_manager, new_selector)
                        else:
                            # Selector unchanged/completed — clear as before
                            chat_manager._setting_selector = None
                            chat_manager._setting_continuation = None
                            clear_active_interaction(chat_manager)
                    continue

                # Boundary approval resolved via toolbar — resume the suspended
                # agentic orchestrator.  The user granted or denied full
                # filesystem access for a path outside project boundaries, and
                # the interaction's done event was set by ToolApprovalPending
                # (with exit_sentinel=134).
                if raw_input == BOUNDARY_RESOLVED_SENTINEL and getattr(chat_manager, '_agentic_orchestrator', None) is not None:
                    INPUT_BLOCKED['blocked'] = True
                    try:
                        _resume_orchestrator_with_live_toolbar(
                            chat_manager,
                            session,
                            safe_console,
                            "resume_after_boundary",
                        )
                    finally:
                        INPUT_BLOCKED['blocked'] = False
                        _drain_stdin(session)
                    continue

                # Command confirmation (yes/no toolbar interaction) resolved
                # via sentinel 135.  The user selected Yes, No, or pressed Esc
                # in a CommandConfirmInteraction.  Read the result and call
                # the stored continuation with the bool result (or None for
                # cancel).
                if raw_input == COMMAND_CONFIRM_SENTINEL and getattr(chat_manager, '_confirm_interaction', None) is not None:
                    interaction = chat_manager._confirm_interaction
                    continuation = getattr(chat_manager, '_confirm_continuation', None)
                    cancelled = interaction.was_cancelled()
                    result = None if cancelled else interaction.result()
                    try:
                        if continuation is not None:
                            continuation(result)
                    finally:
                        chat_manager._confirm_interaction = None
                        chat_manager._confirm_continuation = None
                        clear_active_interaction(chat_manager)
                    continue

                # Pending interaction resolution — intercept submitted text
                # instead of treating it as a normal prompt command.
                pending = chat_manager.get_pending_interaction()
                if pending is not None:
                    raw_text = raw_input
                    chat_manager.resolve_pending_interaction(raw_text)
                    # Call text continuation if this was a command-level text input
                    continuation = getattr(chat_manager, '_pending_text_continuation', None)
                    if continuation is not None:
                        chat_manager._pending_text_continuation = None
                        continuation(raw_text)
                    continue

                user_input = consume_image_attach_lines(raw_input.strip())
                prompt_attachments = list(pending_attachments)
                pending_attachments.clear()

                if not user_input and not prompt_attachments:
                    # Clear the empty input line to avoid multiple prompts stacking
                    import sys
                    sys.stdout.write("\033[F\033[K")  # Move up and clear line
                    sys.stdout.flush()
                    continue

                # Process commands
                cmd_result, modified_input, cmd_worker = process_command(chat_manager, user_input, console, DEBUG_MODE_CONTAINER, cron_scheduler)
                if cmd_result == "exit":
                    break
                elif cmd_result == "swarm_worker":
                    if prompt_attachments:
                        console.print("[dim]Discarded pasted image attachments because slash commands do not use them.[/dim]")
                    if cron_scheduler:
                        cron_scheduler.stop()
                        cron_scheduler = None
                    if chat_manager.swarm_admin_mode and chat_manager.swarm_server:
                        chat_manager.swarm_server.stop()
                        chat_manager.swarm_admin_mode = False
                        chat_manager.swarm_server = None
                    chat_manager.cleanup()
                    handoff_to_worker = True

                    from core.swarm_worker import run_worker_cli
                    return run_worker_cli(
                        swarm_name=modified_input,
                        repo_root=str(Path.cwd()),
                        rg_exe_path=RG_EXE_PATH,
                        host=swarm_settings.host,
                        port=swarm_settings.port,
                    ) or 0
                elif cmd_result == "handled":
                    if prompt_attachments:
                        console.print("[dim]Discarded pasted image attachments because slash commands do not use them.[/dim]")
                    continue
                elif cmd_result == "setting_selector":
                    # Command handler (e.g. /config) stored a SettingSelector
                    # and continuation on chat_manager.  Patch the selector
                    # so finish/cancel exit the prompt app with sentinel 133,
                    # set it as the active toolbar interaction, then loop
                    # back to session.prompt() where it will be rendered.
                    if prompt_attachments:
                        console.print("[dim]Discarded pasted image attachments because slash commands do not use them.[/dim]")
                    selector = getattr(chat_manager, '_setting_selector', None)
                    if selector is not None:
                        patch_for_active_prompt(selector, SETTING_RESOLVED_SENTINEL)
                        set_active_interaction(chat_manager, selector)
                    continue

                elif cmd_result == "confirm_input":
                    # Command handler (e.g. a migrated Confirm.ask) stored a
                    # CommandConfirmInteraction and continuation.  Patch the
                    # interaction so finish/cancel exit with sentinel 135,
                    # set it as the active toolbar interaction, then loop
                    # back to session.prompt() where it will be rendered.
                    if prompt_attachments:
                        console.print("[dim]Discarded pasted image attachments because slash commands do not use them.[/dim]")
                    interaction = getattr(chat_manager, '_confirm_interaction', None)
                    if interaction is not None:
                        patch_for_active_prompt(interaction, COMMAND_CONFIRM_SENTINEL)
                        set_active_interaction(chat_manager, interaction)
                    continue

                elif cmd_result == "text_input":
                    # Command handler (e.g. a migrated Prompt.ask) stored a
                    # PendingInteraction and continuation.  The pending
                    # interaction is already set on chat_manager by the
                    # handoff function.  Just loop back to session.prompt()
                    # where render_pending_interaction() shows the prompt
                    # in the toolbar, and typing text resolves it.
                    if prompt_attachments:
                        console.print("[dim]Discarded pasted image attachments because slash commands do not use them.[/dim]")
                    continue

                elif cmd_result == "subagent_run":
                    # Slash command (/ask, /review) deferred sub-agent work.
                    # Run the worker in a background thread while keeping a
                    # minimal PTK prompt active so the toolbar can render
                    # live subagent status (SubAgentPanel-driven).
                    if prompt_attachments:
                        console.print("[dim]Discarded pasted image attachments because slash commands do not use them.[/dim]")
                    if cmd_worker is None:
                        continue
                    INPUT_BLOCKED['blocked'] = True
                    completion_event = threading.Event()
                    safe_console.set_app(session.app)
                    agent_thread = threading.Thread(
                        target=cmd_worker,
                        args=(console, safe_console, completion_event),
                        daemon=True,
                    )
                    agent_thread.start()
                    try:
                        raw_input = session.prompt(
                            lambda: "",
                            bottom_toolbar=lambda: get_bottom_toolbar_text(chat_manager),
                            inputhook=_create_agent_done_inputhook(
                                completion_event,
                                on_complete=lambda: _stop_progress_spinner(chat_manager),
                            ),
                        )
                    except KeyboardInterrupt:
                        _handle_ctrl_c_bg_prompt(
                            chat_manager, session, safe_console,
                            agent_thread, completion_event,
                            cancel_msg="Subagent cancelled.",
                            idle_msg="Cancelled (Ctrl+C). Press Ctrl+C again to exit.",
                        )
                        continue
                    _cleanup_bg_prompt(chat_manager, session, safe_console, agent_thread)
                    continue

                # Use modified input if provided (from /edit command)
                final_input = modified_input if modified_input else user_input
                final_content = build_message_content(final_input, prompt_attachments)

                chat_manager.maybe_auto_compact(console)

                if TOOLS_ENABLED:
                    # Background thread + living PTK for toolbar progress
                    completion_event = threading.Event()
                    result_holder = {'done': False}
                    safe_console.set_app(session.app)
                    _start_progress_spinner(chat_manager, "Thinking ...")
                    console.print("─" * console.width, style="rgb(30,30,30)")
                    console.print()  # Extra newline after user input to separate from LLM response
                    agent_thread = threading.Thread(
                        target=_run_agent_in_thread,
                        args=(chat_manager, final_content, console, safe_console,
                              Path.cwd().resolve(), RG_EXE_PATH,
                              DEBUG_MODE_CONTAINER['debug'],
                              completion_event, result_holder),
                        daemon=True,
                    )
                    agent_thread.start()
                    try:
                        raw_input = session.prompt(
                            lambda: "",
                            bottom_toolbar=lambda: get_bottom_toolbar_text(chat_manager),
                            inputhook=_create_agent_done_inputhook(
                                completion_event,
                                on_complete=lambda: _stop_progress_spinner(chat_manager),
                            ),
                        )
                    except KeyboardInterrupt:
                        _handle_ctrl_c_bg_prompt(
                            chat_manager, session, safe_console,
                            agent_thread, completion_event,
                            cancel_msg="Subagent cancelled.",
                            idle_msg="Response interrupted (Ctrl+C). Press Ctrl+C again to exit.",
                        )
                        continue
                    _cleanup_bg_prompt(chat_manager, session, safe_console, agent_thread)
                else:
                    thinking_indicator.start()
                    INPUT_BLOCKED['blocked'] = True
                    try:
                        _safe_print(console, session, "─" * console.width, style="rgb(30,30,30)")
                        _safe_print(console, session)  # Extra newline after user input to separate from LLM response
                        chat_manager.messages.append({"role": "user", "content": final_content})
                        chat_manager.log_message({"role": "user", "content": final_content})

                        try:
                            stream = chat_manager.client.chat_completion(
                                chat_manager.messages, stream=True
                            )
                            if isinstance(stream, str):
                                _safe_print(console, session, f"[red]Error: {stream}[/red]")
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
                                # Force toolbar to repaint without spinner before printing
                                chat_manager.invalidate_toolbar()
                                INPUT_BLOCKED['blocked'] = False
                                _drain_stdin(session)

                                if full_response.strip():
                                    md = Markdown(left_align_headings(full_response), code_theme=MonokaiDarkBGStyle, justify="left")
                                    _safe_print(console, session, md)

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
                                    _safe_print(console, session, "\n[yellow]Response interrupted (Ctrl+C). Press Ctrl+C again to exit.[/yellow]")
                                    # Save partial response
                                    if chunks:
                                        partial = "".join(chunks)
                                        if partial.strip():
                                            partial_with_note = partial + "\n\n*[Response interrupted]*"
                                            md = Markdown(left_align_headings(partial_with_note), code_theme=MonokaiDarkBGStyle, justify="left")
                                            _safe_print(console, session, md)
                                            chat_manager.messages.append(
                                                {"role": "assistant", "content": partial}
                                            )
                                _safe_print(console, session)  # Extra spacing
                            finally:
                                # Ensure HTTP connection is closed
                                if hasattr(stream, 'close'):
                                    stream.close()

                        except BoneAgentError as e:
                            # Handle all bone-agent custom exceptions gracefully
                            _safe_print(console, session, f"Error: {e}", style="red", markup=False)
                            if hasattr(e, 'details') and e.details:
                                _safe_print(console, session, f"Details: {e.details}", style="dim", markup=False)
                        except Exception as e:
                            _safe_print(console, session, f"[red]Error during generation: {e}[/red]", markup=False)
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
        if not handoff_to_worker:
            # Display session summary before cleanup
            summary = chat_manager.token_tracker.get_session_summary()
            console.print(f"\n[white]Session Summary: {summary}[/white]")

            # Stop cron scheduler if running
            if cron_scheduler:
                cron_scheduler.stop()

            # Stop swarm server if active
            if chat_manager.swarm_admin_mode and chat_manager.swarm_server:
                console.print("[dim]Stopping swarm server...[/dim]")
                try:
                    chat_manager.swarm_server.stop()
                except Exception:
                    pass
                chat_manager.swarm_admin_mode = False
                chat_manager.swarm_server = None

            # Stop background inbox poller
            chat_manager.stop_swarm_inbox_poller()

            chat_manager.cleanup()
            console.print("[yellow]Goodbye![/yellow]")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="bone-agent CLI")
    parser.add_argument("--cron-run", metavar="JOB_ID", help="Run a cron job headlessly and exit")
    parser.add_argument("--worker", metavar="SWARM_NAME", help="Run as a swarm worker (joins the named swarm)")
    parser.add_argument("--swarm-host", default="127.0.0.1", help="Swarm server host for --worker")
    parser.add_argument("--swarm-port", type=int, default=8765, help="Swarm server port for --worker")
    args = parser.parse_args()

    if args.cron_run:
        from core.cron import run_job_headless
        sys.exit(run_job_headless(args.cron_run))

    if args.worker:
        from core.swarm_worker import run_worker_cli
        from utils.paths import RG_EXE_PATH
        sys.exit(
            run_worker_cli(
                swarm_name=args.worker,
                repo_root=str(Path.cwd()),
                rg_exe_path=RG_EXE_PATH,
                host=args.swarm_host,
                port=args.swarm_port,
            )
        )

    sys.exit(main() or 0)
