"""Shared toolbar interaction framework for prompt_toolkit bottom toolbar.

This module provides a lightweight protocol for rendering interactive
selectors, confirmations, and other widgets in the bottom toolbar area
of a prompt_toolkit session -- avoiding nested ``Application.run()`` calls
when possible, and falling back to a minimal controlled Application when
the main prompt loop is not active.

Runtime model
-------------
There are two distinct caller contexts, and **the wrong choice will hang**:

**Active-prompt context** (main ``PromptSession.prompt()`` IS running):
    The main prompt loop's ``bottom_toolbar`` callback and key bindings
    are actively dispatching.  Callers in this context use
    ``wait_for_interaction(chat_manager, interaction)``, which stores the
    interaction on ``chat_manager`` and blocks on a ``threading.Event``
    while the main loop handles rendering and input forwarding.

    → Use ``wait_for_interaction()`` from: key-binding callbacks,
      toolbar callbacks, or any code invoked while ``session.prompt()``
      is on the call stack.

**Agent-turn context** (NO main ``PromptSession.prompt()`` active):
    The main prompt loop is NOT running — e.g. during tool execution
    inside ``agentic_answer()``, sub-agent dispatch, or cron jobs.
    ``get_app().invalidate()`` is a no-op and no key bindings exist to
    forward input.  ``wait_for_interaction()`` would block forever
    (event never set).

    → Use ``run_toolbar_interaction(interaction)`` from: tool handlers,
      sub-agent workers, agent-turn orchestration code, or any code
      that runs while the user is not sitting at the main ``>`` prompt.

The fallback runner creates a minimal prompt_toolkit ``Application``
(no ``PromptSession``, no visible prompt text) that renders the
interaction content exclusively in ``bottom_toolbar``.  It does **not**
use alt screen or Rich Live output, and it does **not** write anything
into the chat transcript.

**Pending-interaction context** (agent-turn code that wants to yield):
    Instead of blocking or running a nested Application, agent-turn code
    can stage a ``PendingInteraction`` on the ``ChatManager`` and return.
    The main prompt loop detects the pending interaction, presents it to
    the user, and calls ``resolve_pending_interaction()`` with the
    response.  The pending interaction's ``_event`` is then set so the
    waiting agent code can resume.

    → Use ``PendingInteraction`` + ``chat_manager.set_pending_interaction()``
      from: tool handlers, sub-agent workers, or any agent-turn code
      that should yield to the main prompt instead of blocking.

Usage (for follow-up workers)
-----------------------------
1. Subclass ``ToolbarInteraction`` and implement ``render()`` and
   ``handle_key()``.
2. Determine caller context:

   *Active prompt?*  → ``wait_for_interaction(chat_manager, my_interaction)``
   *Agent turn?*     → ``run_toolbar_interaction(my_interaction)``

3. Active-prompt path: the main prompt key bindings call
   ``dispatch_toolbar_key(event, chat_manager)`` and the bottom toolbar
   callback calls ``render_active_interaction(chat_manager)`` (wired by
   later workers in ``main.py`` and ``prompt_utils.py``).
4. Agent-turn path: the fallback Application handles everything
   internally — no wiring needed.

5. **Pending (yield-to-prompt) path**::

       pending = PendingInteraction(prompt="Which file?")
       chat_manager.set_pending_interaction(pending)
       # Yield to main prompt loop ...
       pending.wait()          # block until resolved
       answer = pending.result # the user's response

   The main prompt loop (or input hook) is responsible for detecting
   the pending interaction via ``chat_manager.get_pending_interaction()``,
   presenting the prompt, and calling
   ``chat_manager.resolve_pending_interaction(response)``.

Public API
----------
Classes:
    ToolbarInteraction
    PendingInteraction
    CommandConfirmInteraction

Sentinels:
    APPROVAL_RESOLVED_SENTINEL (131)
    SELECTION_RESOLVED_SENTINEL (132)
    SETTING_RESOLVED_SENTINEL (133)
    BOUNDARY_RESOLVED_SENTINEL (134)
    COMMAND_CONFIRM_SENTINEL (135)

State helpers (on ChatManager):
    chat_manager.set_pending_interaction(interaction)
    chat_manager.get_pending_interaction() -> PendingInteraction | None
    chat_manager.clear_pending_interaction()
    chat_manager.resolve_pending_interaction(result) -> bool

Legacy state helpers (duck-typed on ChatManager):
    set_active_interaction(chat_manager, interaction)
    get_active_interaction(chat_manager)
    clear_active_interaction(chat_manager)

Rendering:
    render_active_interaction(chat_manager) -> str | None

Key dispatch:
    dispatch_toolbar_key(event, chat_manager) -> bool

Blocking wait (active prompt):
    wait_for_interaction(chat_manager, interaction, timeout=None) -> Any

Fallback runner (agent turn):
    run_toolbar_interaction(interaction, timeout=None,
                            chat_manager=None) -> Any

Formatting helpers:
    escape_html(text) -> str
    styled(text, fg=None, bold=False) -> str
    make_section(title, lines, footer) -> str
"""

from __future__ import annotations

import threading
from html import escape as _html_escape
from typing import Any, Optional, Tuple

# Sentinel returned by session.prompt() when a tool approval is resolved
# via the active toolbar interaction (user selected accept/deny/advise).
APPROVAL_RESOLVED_SENTINEL = 131

# Sentinel returned by session.prompt() when a select_option interaction
# is resolved via the active toolbar interaction (user selected an option
# or cancelled).
SELECTION_RESOLVED_SENTINEL = 132

# Sentinel returned by session.prompt() when a setting-selector interaction
# (e.g. /config, /provider, /tools, /obsidian) is resolved via the active
# toolbar interaction.  The main loop detects it and calls the stored
# continuation callback to apply the changes.
SETTING_RESOLVED_SENTINEL = 133

# Sentinel returned by session.prompt() when a boundary-approval interaction
# is resolved via the active toolbar interaction (user granted or denied
# full filesystem access for a path outside project boundaries).
BOUNDARY_RESOLVED_SENTINEL = 134

# Sentinel returned by session.prompt() when a command-level confirmation
# (yes/no toolbar interaction via ``confirm_handoff()``) is resolved.
# The main loop detects it and calls the stored continuation with the
# boolean result (True=yes, False=no, None=cancelled).
COMMAND_CONFIRM_SENTINEL = 135

from prompt_toolkit import HTML
from prompt_toolkit.application import Application, get_app
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, Window, HSplit
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style


# ---------------------------------------------------------------------------
# Shared idempotent-exit helpers
# ---------------------------------------------------------------------------

def _exit_app_safe(sentinel: int) -> None:
    """Call ``get_app().exit(sentinel)``, silently ignoring double-exit errors.

    prompt_toolkit raises ``ReturnValue`` (or ``ApplicationError``) if
    ``Application.exit()`` is called more than once on the same app run.
    This wrapper lets callers avoid those crashes when multiple key events
    race to resolve an interaction.
    """
    try:
        get_app().exit(result=sentinel)
    except Exception:
        pass


def _make_idempotent_exit(interaction: "ToolbarInteraction", sentinel: int) -> None:
    """Wrap *interaction*'s ``finish``/``cancel`` with an ``is_done()`` guard.

    After the first call to ``finish()`` or ``cancel()``, ``is_done()``
    returns ``True`` and subsequent calls return immediately without
    touching ``finish``/``cancel`` or calling ``get_app().exit()``.

    This is the primary defence against the double-exit crash.  The
    ``is_done()`` guard is checked **before** the original method so
    that the interaction's state (``_result``, ``_done_event``) is never
    mutated more than once and ``get_app().exit()`` is invoked exactly
    once per resolution.
    """
    _orig_finish = interaction.finish
    _orig_cancel = interaction.cancel

    def _patched_finish(result: Any = None) -> None:
        if interaction.is_done():
            return
        _orig_finish(result)
        _exit_app_safe(sentinel)

    def _patched_cancel() -> None:
        if interaction.is_done():
            return
        _orig_cancel()
        _exit_app_safe(sentinel)

    interaction.finish = _patched_finish  # type: ignore[method-assign]
    interaction.cancel = _patched_cancel  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Active-prompt patching helper
# ---------------------------------------------------------------------------

def patch_for_active_prompt(interaction: "ToolbarInteraction", sentinel: int) -> None:
    """Monkey-patch *interaction* so finish/cancel exit the main prompt app.

    In the active-prompt context (``session.prompt()`` IS running), the
    interaction needs to call ``get_app().exit(sentinel)`` when the user
    completes or cancels, so that ``session.prompt()`` returns the sentinel
    value and the main loop can detect the resolution.

    This is equivalent to what ``ToolApprovalPending._resolve()`` does,
    and uses the shared ``_make_idempotent_exit()`` helper to guard
    against double-exit crashes when multiple key events race.

    Call this before storing the interaction as the active interaction
    (via ``set_active_interaction()``) in the main prompt path.
    """
    _make_idempotent_exit(interaction, sentinel)


# ---------------------------------------------------------------------------
# Protocol-style base class
# ---------------------------------------------------------------------------

class ToolbarInteraction:
    """Base class for a toolbar-hosted interactive widget.

    Subclasses must implement ``render()`` and ``handle_key()``.
    Call ``finish(result)`` or ``cancel()`` to signal completion from
    within ``handle_key()``.
    """

    def __init__(self) -> None:
        self._done_event = threading.Event()
        self._result: Any = None
        self._cancelled: bool = False

    # --- Subclass interface -------------------------------------------------

    def render(self) -> str:
        """Return HTML-formatted text for the toolbar area.

        Return a string suitable for prompt_toolkit's ``HTML()`` class.
        Escaping of user-supplied text is the subclass's responsibility
        (use ``escape_html()`` from this module).
        """
        raise NotImplementedError

    def handle_key(self, event: Any) -> bool:
        """Handle a key event forwarded from the main prompt session.

        Args:
            event: A prompt_toolkit ``KeyPressEvent``.

        Returns:
            True if the event was consumed, False to let other bindings
            handle it.
        """
        raise NotImplementedError

    def cleanup(self) -> None:
        """Optional hook called when the interaction is removed.

        Override to release resources, kill timers, etc.
        """
        pass

    # --- Completion helpers -------------------------------------------------

    def finish(self, result: Any = None) -> None:
        """Signal successful completion with an optional result."""
        self._result = result
        self._cancelled = False
        self._done_event.set()

    def cancel(self) -> None:
        """Signal cancellation (equivalent to Esc / user abort)."""
        self._result = None
        self._cancelled = True
        self._done_event.set()

    def is_done(self) -> bool:
        """Return True if the interaction has finished or been cancelled."""
        return self._done_event.is_set()

    def result(self) -> Any:
        """Return the result value (or None if cancelled)."""
        return self._result

    def was_cancelled(self) -> bool:
        """Return True if the interaction was cancelled."""
        return self._cancelled


# ---------------------------------------------------------------------------
# Tool approval interaction (active-prompt context, exits on resolve)
# ---------------------------------------------------------------------------

class ToolApprovalPending(ToolbarInteraction):
    """Tool approval interaction for the active-prompt context.

    Renders a toolbar with arrow-key navigation for accept/advise/cancel
    (and Accept All Edits for edit tools).  When the user selects an
    option, ``finish()`` is called **and** ``get_app().exit(131)`` fires
    so that ``session.prompt()`` returns the sentinel.  The caller reads
    ``.result`` to obtain the ``(action, guidance)`` tuple.

    This is the suspension-path counterpart to ``_ToolApprovalInteraction``
    (which is run via ``run_toolbar_interaction()`` in the blocking path).
    """

    _CURSOR = "> "

    def __init__(
        self,
        tool_command: str,
        reason: Optional[str],
        is_edit_tool: bool = False,
        cycle_approve_mode: Any = None,
        exit_sentinel: int = APPROVAL_RESOLVED_SENTINEL,
    ) -> None:
        super().__init__()
        self._tool_command = tool_command
        self._reason = reason
        self._is_edit_tool = is_edit_tool
        self._cycle_approve_mode = cycle_approve_mode
        self._exit_sentinel = exit_sentinel
        self._selected_index = 0
        self._advice_buffer = ""
        self._mode = "navigate"  # "navigate" | "advise"

        self._options: list[dict] = (
            [
                {"value": "accept", "text": "Accept"},
                {"value": "accept_all_edits", "text": "Accept All Edits"},
                {"value": "advise", "text": "Advise"},
                {"value": "cancel", "text": "Cancel"},
            ]
            if is_edit_tool
            else [
                {"value": "accept", "text": "Accept"},
                {"value": "advise", "text": "Advise"},
                {"value": "cancel", "text": "Cancel"},
            ]
        )

    # -- Rendering ----------------------------------------------------------

    def render(self) -> str:
        if self._mode == "advise":
            return self._render_advise()
        return self._render_navigate()

    def _render_navigate(self) -> str:
        lines: list[str] = []

        cmd_display = self._tool_command
        if len(cmd_display) > 72:
            cmd_display = cmd_display[:69] + "..."
        lines.append(styled(escape_html(cmd_display), fg="#cad2d9", bold=True))

        if self._reason:
            reason_display = self._reason.splitlines()[0]
            if len(reason_display) > 90:
                reason_display = reason_display[:87] + "..."
            lines.append(styled(escape_html(reason_display), fg="#888888"))

        option_texts: list[str] = []
        for idx, opt in enumerate(self._options):
            text = opt.get("text", "")
            if idx == self._selected_index:
                option_texts.append(
                    styled(f"{self._CURSOR}{text}", fg="#FFFFFF", bold=True)
                )
            else:
                option_texts.append(styled(f"  {text}", fg="#6a737d"))
        lines.append("   ".join(option_texts))

        return make_section(lines=lines)

    def _render_advise(self) -> str:
        lines: list[str] = []
        lines.append(
            styled("Enter advice (\u21b5 confirm, Esc back):", fg="#cad2d9", bold=True)
        )
        buf = self._advice_buffer
        cursor = styled("\u258c", fg="#FFFFFF")
        if buf:
            lines.append(styled(escape_html(buf) + cursor, fg="#FFFFFF"))
        else:
            lines.append(cursor)
        return make_section(lines=lines)

    # -- Key handling -------------------------------------------------------

    def handle_key(self, event: Any) -> bool:
        if self._mode == "advise":
            return self._handle_advise_key(event)
        return self._handle_navigate_key(event)

    def _handle_navigate_key(self, event: Any) -> bool:
        name = self._key_name(event)

        if name in ("up", "left"):
            if self._selected_index > 0:
                self._selected_index -= 1
                self._safe_invalidate(event)
            return True

        if name in ("down", "right"):
            if self._selected_index < len(self._options) - 1:
                self._selected_index += 1
                self._safe_invalidate(event)
            return True

        if name == "enter":
            selected_value = self._options[self._selected_index].get("value")
            if selected_value == "accept_all_edits":
                if self._cycle_approve_mode:
                    self._cycle_approve_mode("accept_edits")
                self._resolve(("accept", None))
            elif selected_value == "advise":
                self._mode = "advise"
                self._advice_buffer = ""
                self._safe_invalidate(event)
            else:
                self._resolve((selected_value, None))
            return True

        if name == "escape":
            self._resolve(("cancel", None))
            return True

        data = self._key_data(event)
        if data and len(data) == 1:
            letter = data.lower()
            for idx, opt in enumerate(self._options):
                opt_text = opt.get("text", "")
                if opt_text.lower().startswith(letter):
                    self._selected_index = idx
                    self._safe_invalidate(event)
                    break
            return True

        return True

    def _handle_advise_key(self, event: Any) -> bool:
        name = self._key_name(event)

        if name == "escape":
            self._mode = "navigate"
            self._advice_buffer = ""
            self._safe_invalidate(event)
            return True

        if name == "enter":
            advice = self._advice_buffer.strip()
            if not advice:
                self._resolve(("cancel", None))
            else:
                self._resolve(("advise", advice))
            return True

        if name == "backspace":
            self._advice_buffer = self._advice_buffer[:-1]
            self._safe_invalidate(event)
            return True

        data = self._key_data(event)
        if data and len(data) == 1:
            self._advice_buffer += data
            self._safe_invalidate(event)
            return True

        return True

    # -- Resolution ---------------------------------------------------------

    def _resolve(self, result: Tuple[str, Optional[str]]) -> None:
        """Finish the interaction AND exit the prompt app so control returns."""
        if self.is_done():
            return
        self.finish(result)
        _exit_app_safe(self._exit_sentinel)

    # -- Key extraction helpers ---------------------------------------------

    @staticmethod
    def _normalize_key_name(name: str) -> str:
        name = name.lower()
        if name in ("c-m", "controlm", "\r", "\n"):
            return "enter"
        if name in ("c-h", "controlh"):
            return "backspace"
        if name in ("c-i", "controli", "\t"):
            return "tab"
        if name in ("space",):
            return " "
        if name in ("delete", "c-d"):
            return "delete"
        return name

    @staticmethod
    def _key_name(event: Any) -> Optional[str]:
        try:
            seq = event.key_sequence
            if seq:
                press = seq[-1]
                key = getattr(press, "key", None)
                name = getattr(key, "name", None) if key is not None else None
                if not name:
                    name = getattr(press, "data", None)
                return ToolApprovalPending._normalize_key_name(name) if name else None
        except Exception:
            pass
        return None

    @staticmethod
    def _key_data(event: Any) -> Optional[str]:
        try:
            seq = event.key_sequence
            if seq:
                press = seq[-1]
                data = getattr(press, "data", None)
                return data if data else None
        except Exception:
            pass
        return None

    @staticmethod
    def _safe_invalidate(event: Any) -> None:
        try:
            event.app.invalidate()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Pending interaction contract (non-blocking, yields to main prompt)
# ---------------------------------------------------------------------------

class PendingInteraction:
    """A request for user input that yields to the main prompt loop.

    Unlike ``ToolbarInteraction`` (which blocks on an event or runs a
    nested Application), a ``PendingInteraction`` is staged on the
    ``ChatManager`` and resolved later when the main prompt loop picks it
    up.  This enables agent-turn code to request input without blocking
    or launching a nested prompt_toolkit app.

    Usage (for follow-up workers)::

        pending = PendingInteraction(prompt="Which file?")
        chat_manager.set_pending_interaction(pending)
        # Yield control to the main prompt loop ...
        # Later, when the user responds:
        pending.wait()             # or use the event directly
        result = pending.result    # the user's answer (or None if cancelled)

    The main prompt loop (or input hook) is responsible for detecting the
    pending interaction, presenting it to the user, and calling
    ``pending.resolve(response)`` with the user's input.
    """

    def __init__(self, prompt: str) -> None:
        self.prompt: str = prompt
        self._result: Any = None
        self._resolved: bool = False
        self._event: threading.Event = threading.Event()

    def resolve(self, result: Any = None) -> None:
        """Complete the pending interaction with an optional result."""
        self._result = result
        self._resolved = True
        self._event.set()

    def cancel(self) -> None:
        """Cancel the pending interaction (result stays None)."""
        self._resolved = True
        self._event.set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until resolved or cancelled.

        Returns True if resolved/cancelled, False on timeout.
        """
        return self._event.wait(timeout=timeout)

    @property
    def is_resolved(self) -> bool:
        """Return True if the interaction has been resolved or cancelled."""
        return self._resolved

    @property
    def result(self) -> Any:
        """Return the resolved result (or None if cancelled/not yet resolved)."""
        return self._result


# ---------------------------------------------------------------------------
# Command-level confirmation interaction (yes/no in toolbar)
# ---------------------------------------------------------------------------

class CommandConfirmInteraction(ToolbarInteraction):
    """Simple yes/no confirmation rendered in the bottom toolbar.

    Renders a prompt message with selectable Yes/No options navigated
    via arrow keys.  Enter selects the current option; Escape cancels
    (treated as ``finish(None)``).

    The main loop detects resolution via ``COMMAND_CONFIRM_SENTINEL``
    (135) and calls the stored continuation with the result.

    Usage (from command handlers in ``commands.py``)::

        interaction = CommandConfirmInteraction(
            "Install foo@1.0.0 now?"
        )
        chat_manager._confirm_interaction = interaction
        chat_manager._confirm_continuation = lambda result: ...
        return CommandResult(status="confirm_input")

    Or use the ``_confirm_handoff()`` helper in ``commands.py``.
    """

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self._prompt = prompt
        self._selected_index: int = 0

    # -- Rendering ----------------------------------------------------------

    def render(self) -> str:
        lines: list[str] = []
        lines.append(styled(escape_html(self._prompt), fg="#cad2d9", bold=True))
        lines.append("")

        def _option(text: str, selected: bool) -> str:
            if selected:
                return styled(f"> {text}", fg="#FFFFFF", bold=True)
            return styled(f"  {text}", fg="#6a737d")

        yes_text = _option("Yes", self._selected_index == 0)
        no_text = _option("No", self._selected_index == 1)
        lines.append(f"{yes_text}   {no_text}")

        lines.append(
            styled("\u2190\u2191\u2192/\u2191\u2193 navigate  \u21b5 select  Esc cancel", fg="#555555")
        )
        return make_section(lines=lines)

    # -- Key handling -------------------------------------------------------

    def handle_key(self, event: Any) -> bool:
        name = self._key_name(event)

        if name in ("up", "left"):
            self._selected_index = 0
            self._safe_invalidate(event)
            return True

        if name in ("down", "right"):
            self._selected_index = 1
            self._safe_invalidate(event)
            return True

        if name == "enter":
            if self._selected_index == 0:
                self.finish(True)
            else:
                self.finish(False)
            return True

        if name == "escape":
            self.cancel()
            return True

        # Consume all other keys to prevent accidental prompt text entry.
        return True

    # -- Key extraction helpers (mirrors ToolApprovalPending) ---------------

    @staticmethod
    def _key_name(event: Any) -> str | None:
        try:
            seq = event.key_sequence
            if seq:
                press = seq[-1]
                key = getattr(press, "key", None)
                name = getattr(key, "name", None) if key is not None else None
                if not name:
                    name = getattr(press, "data", None)
                return ToolApprovalPending._normalize_key_name(name) if name else None
        except Exception:
            pass
        return None

    @staticmethod
    def _safe_invalidate(event: Any) -> None:
        try:
            event.app.invalidate()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# State management (duck-typed on chat_manager, no ChatManager edits needed)
# ---------------------------------------------------------------------------

_INTERACTION_ATTR = "_toolbar_interaction"


def set_active_interaction(
    chat_manager: Any, interaction: ToolbarInteraction
) -> None:
    """Store *interaction* as the active toolbar interaction."""
    setattr(chat_manager, _INTERACTION_ATTR, interaction)


def get_active_interaction(chat_manager: Any) -> Optional[ToolbarInteraction]:
    """Return the active toolbar interaction, or None."""
    return getattr(chat_manager, _INTERACTION_ATTR, None)


def clear_active_interaction(chat_manager: Any) -> None:
    """Remove the active interaction, calling its ``cleanup()`` hook."""
    interaction = get_active_interaction(chat_manager)
    if interaction is not None:
        try:
            interaction.cleanup()
        except Exception:
            pass
    try:
        delattr(chat_manager, _INTERACTION_ATTR)
    except AttributeError:
        pass


# ---------------------------------------------------------------------------
# Rendering helper (for integration into get_bottom_toolbar_text)
# ---------------------------------------------------------------------------

def render_active_interaction(chat_manager: Any) -> Optional[str]:
    """Return HTML-formatted content for the active interaction, if any.

    Returns None when there is no active interaction, so callers can fall
    through to the standard toolbar text.
    """
    interaction = get_active_interaction(chat_manager)
    if interaction is None:
        return None
    return interaction.render()


# ---------------------------------------------------------------------------
# Key dispatch (for integration into main prompt key bindings)
# ---------------------------------------------------------------------------

def dispatch_toolbar_key(event: Any, chat_manager: Any) -> bool:
    """Forward a key event to the active toolbar interaction.

    Returns True if the event was consumed (caller should not process
    further).

    Usage inside a prompt_toolkit key binding::

        if dispatch_toolbar_key(event, chat_manager):
            return
    """
    interaction = get_active_interaction(chat_manager)
    if interaction is None:
        return False
    return interaction.handle_key(event)


# ---------------------------------------------------------------------------
# Blocking wait helper
# ---------------------------------------------------------------------------

def wait_for_interaction(
    chat_manager: Any,
    interaction: ToolbarInteraction,
    timeout: Optional[float] = None,
    invalidate_app: bool = True,
) -> Any:
    """Block the calling thread until *interaction* completes.

    **ACTIVE-PROMPT CONTEXT ONLY.**  The main ``PromptSession.prompt()``
    must be active so that key bindings and the toolbar callback forward
    input and rendering to the interaction.  Calling this from agent-turn
    code (tool execution, sub-agent dispatch) will hang — use
    ``run_toolbar_interaction()`` instead.  See the module docstring for
    the full runtime model.

    Stores the interaction on *chat_manager*, invalidates the prompt app
    so the toolbar re-renders, then blocks on a threading event.  When
    ``interaction.finish()`` or ``interaction.cancel()`` is called (from
    a key binding callback dispatched by the main prompt loop), this
    function returns.

    Args:
        chat_manager: The ChatManager instance.
        interaction: A configured ``ToolbarInteraction`` instance.
        timeout: Optional timeout in seconds.  If exceeded, cancels the
                 interaction and returns None.
        invalidate_app: Try to call ``get_app().invalidate()`` to force
                        a toolbar re-render (safe when no app is active).

    Returns:
        The interaction's result, or None if cancelled/timed out.

    Raises:
        TimeoutError: If *timeout* is reached (after cancelling the
                      interaction).
    """
    set_active_interaction(chat_manager, interaction)

    # Invalidate the prompt app so the bottom_toolbar callback re-fires.
    if invalidate_app:
        _safe_invalidate()

    try:
        finished = interaction._done_event.wait(timeout=timeout)
        if not finished:
            interaction.cancel()
            clear_active_interaction(chat_manager)
            if invalidate_app:
                _safe_invalidate()
            raise TimeoutError(
                f"Toolbar interaction timed out after {timeout:.1f}s"
            )

        if interaction.was_cancelled():
            return None

        return interaction.result()
    finally:
        clear_active_interaction(chat_manager)
        if invalidate_app:
            _safe_invalidate()


# ---------------------------------------------------------------------------
# Fallback runner (agent-turn context — no active PromptSession.prompt())
# ---------------------------------------------------------------------------

# Inline copy of TOOLBAR_STYLE from prompt_utils.py to avoid cross-module
# import cycles.  Kept in sync manually.
_FALLBACK_STYLE = Style.from_dict({
    "bottom-toolbar": "bg:default fg:#FFFFFF noreverse",
    "bottom-toolbar.text": "bg:default fg:#FFFFFF noreverse",
})


def run_toolbar_interaction(
    interaction: ToolbarInteraction,
    timeout: Optional[float] = None,
    chat_manager: Any = None,
) -> Any:
    """Run *interaction* in a minimal prompt_toolkit Application.

    **AGENT-TURN CONTEXT.**  Use this when no main ``PromptSession.prompt()``
    is active — e.g. during tool execution inside ``agentic_answer()``,
    sub-agent dispatch, or any code that runs while the user is not sitting
    at the main ``>`` prompt.

    Creates a minimal ``Application`` with an empty prompt area and the
    interaction content rendered exclusively in ``bottom_toolbar``.
    Nothing is written to the chat transcript.  Does **not** use alt
    screen (``full_screen=False``).

    Monkey-patches ``interaction.finish()`` and ``interaction.cancel()``
    to also call ``get_app().exit()`` so the application terminates
    cleanly when the interaction completes.  Original methods are
    restored in a ``finally`` block before returning.

    Args:
        interaction: A configured ``ToolbarInteraction`` instance.
        timeout: Optional timeout in seconds.  If exceeded, cancels the
                 interaction and returns None.
        chat_manager: Optional ChatManager.  When provided, the application
                      is run via ``_run_application_interruptible`` so
                      pending swarm admin approvals can interrupt the
                      interaction (returns None on interrupt).

    Returns:
        The interaction's result, or None if cancelled / timed out /
        interrupted by swarm admin work.

    Raises:
        TimeoutError: If *timeout* is reached (after cancelling the
                      interaction).
    """
    # ------------------------------------------------------------------
    # Monkey-patch finish/cancel to also call get_app().exit().
    # Save originals first so the finally-block can restore them.
    # ------------------------------------------------------------------
    _orig_finish = interaction.finish
    _orig_cancel = interaction.cancel

    # Use the shared helper which applies the is_done() guard, preventing
    # double-exit when cancel fires more than once (e.g. Escape + timeout
    # race, or swarm interrupt after normal resolution).
    _make_idempotent_exit(interaction, None)

    timeout_timer: Optional[threading.Timer] = None

    try:
        # --------------------------------------------------------------
        # Key bindings: forward all relevant keys to the interaction.
        # String key names match prompt_utils.py for reliability.
        # --------------------------------------------------------------
        bindings = KeyBindings()

        # Navigation / action keys.
        for key in ('up', 'down', 'left', 'right',
                    'enter', 'escape', 'tab', 's-tab',
                    'backspace', 'delete', 'space'):
            @bindings.add(key)
            def _handler(event: Any) -> None:
                interaction.handle_key(event)

        # Printable characters and other single-key input.
        @bindings.add('<any>')
        def _any_key(event: Any) -> None:
            interaction.handle_key(event)

        # --------------------------------------------------------------
        # Layout: HSplit with a filler body above, interaction at the bottom.
        # This places the interaction near the terminal footer without using
        # the unsupported bottom_toolbar= kwarg.
        # --------------------------------------------------------------
        def _interaction_content() -> HTML:
            try:
                return HTML(interaction.render())
            except Exception:
                return HTML("<style fg='red'>Interaction render error</style>")

        filler = Window(content=FormattedTextControl(''))
        interaction_window = Window(
            content=FormattedTextControl(_interaction_content),
        )
        root = HSplit([filler, interaction_window])
        layout = Layout(root)

        application = Application(
            layout=layout,
            key_bindings=bindings,
            full_screen=False,
            style=_FALLBACK_STYLE,
            mouse_support=False,
        )

        # --------------------------------------------------------------
        # Optional timeout.
        # --------------------------------------------------------------
        if timeout is not None:

            def _on_timeout() -> None:
                interaction.cancel()
                try:
                    get_app().exit(result=None)
                except Exception:
                    pass

            timeout_timer = threading.Timer(timeout, _on_timeout)
            timeout_timer.daemon = True
            timeout_timer.start()

        # --------------------------------------------------------------
        # Run the application.
        # --------------------------------------------------------------
        if chat_manager is not None:
            from ui.prompt_interrupts import _run_application_interruptible

            result = _run_application_interruptible(application, chat_manager)
        else:
            result = application.run()

        # 130 is the sentinel for "interrupted by pending swarm approval".
        if result == 130:
            interaction.cancel()
            return None

        if interaction.was_cancelled():
            return None

        return interaction.result()

    finally:
        # Cancel pending timeout.
        if timeout_timer is not None:
            timeout_timer.cancel()

        # Restore original finish/cancel.
        interaction.finish = _orig_finish  # type: ignore[method-assign]
        interaction.cancel = _orig_cancel  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def escape_html(text: str) -> str:
    """HTML-escape user-supplied text for safe toolbar rendering.

    Wraps ``html.escape`` for convenience so callers don't need a
    separate import just for escaping.
    """
    return _html_escape(text)


def styled(text: str, fg: Optional[str] = None, bold: bool = False) -> str:
    """Return an HTML ``<style>``-wrapped string for toolbar rendering.

    Args:
        text: Already-escaped display text.
        fg: Optional CSS colour (e.g. ``"#888888"`` or ``"gray"``).
        bold: If True, wrap in ``<b>`` as well.

    Returns:
        A string suitable for embedding in an ``HTML()`` body.

    Example::

        styled("Press Enter", fg="#5F9EA0", bold=True)
        # => '<style fg="#5F9EA0"><b>Press Enter</b></style>'
    """
    if fg is not None:
        if bold:
            return f'<style fg="{fg}"><b>{text}</b></style>'
        return f'<style fg="{fg}">{text}</style>'
    if bold:
        return f"<b>{text}</b>"
    return text


def make_section(
    title: Optional[str] = None,
    lines: Optional[list[str]] = None,
    footer: Optional[str] = None,
) -> str:
    """Build a compact toolbar section from a title, content, and footer.

    Each line is joined with ``\\n``.  None arguments are omitted.

    Args:
        title: Optional bold title line (already escaped/styled).
        lines: Optional list of content strings (already escaped/styled).
        footer: Optional dim footer line (already escaped/styled).

    Returns:
        A newline-joined string suitable for ``HTML()``.
    """
    parts: list[str] = []
    if title:
        parts.append(title)
    if lines:
        parts.extend(lines)
    if footer:
        parts.append(footer)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Pending interaction rendering
# ---------------------------------------------------------------------------

def render_pending_interaction(chat_manager: Any) -> Optional[str]:
    """Return HTML-formatted content for the pending interaction, if any.

    Returns None when there is no pending interaction.
    """
    pending = getattr(chat_manager, "_pending_interaction", None)
    if pending is None:
        return None
    return (
        f'<style fg="#aaaaaa">Input needed: {_html_escape(pending.prompt)} '
        f'(Enter submits)</style>'
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_invalidate() -> None:
    """Invalidate the current prompt_toolkit Application if one is active.

    Silently ignores errors when no application is running (e.g. during
    startup, tests, or non-interactive use).
    """
    try:
        get_app().invalidate()
    except Exception:
        pass
