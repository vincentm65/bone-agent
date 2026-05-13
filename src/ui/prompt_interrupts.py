"""Shared helpers for making prompt_toolkit prompts interruptible by swarm admin events.

These utilities let nested blocking prompts (select_option, tool_confirmation, etc.)
yield control when a pending swarm approval arrives, so the main loop's auto-turn
handler can process it promptly.

prompt_toolkit 3.x ``InputHookContext`` only exposes ``fileno`` and
``input_is_ready`` — there is no ``app`` attribute.  We use two
complementary mechanisms instead:

1. **pre_run hook** — creates an asyncio background task that polls
   ``has_pending_swarm_work()`` every 100ms and calls ``get_app().exit(130)``
   from inside the event loop.  This is reliable because the coroutine's
   ``asyncio.sleep`` ensures the event-loop selector has a short timeout,
   so ``exit()`` takes effect within one poll cycle.

2. **inputhook** — legacy fallback that returns early when swarm work is
   detected.  On its own it cannot interrupt the selector thread (which
   blocks on ``select(timeout=None)``), but it avoids wasted CPU spinning
   when ``pre_run`` is unavailable (e.g. nested ``Application.run()``
   without ``pre_run`` support).

.. note::
   ``_run_application_interruptible`` uses only the inputhook path because
   ``Application.run()`` does not accept ``pre_run`` — but nested prompts
   (select_option, tool_confirmation) are short-lived enough that the
   inputhook poll + selector timeout combination is acceptable.
"""

import asyncio
import time

from prompt_toolkit.application import get_app


def _save_current_buffer_for_restore(chat_manager):
    """Save in-progress typed buffer content for later prompt restoration.

    Called from interrupt paths before ``get_app().exit(130)`` to preserve
    user input that was being typed when the swarm auto-turn arrived.  This
    must not enqueue the text as a submitted user message: swarm interrupts
    can happen while the user is still drafting input and has not pressed
    Enter.

    Args:
        chat_manager: ChatManager instance (may be None).

    Returns:
        ``True`` if buffer text was saved, ``False`` otherwise.
    """
    if chat_manager is None:
        return False
    try:
        app = get_app()
    except Exception:
        return False
    try:
        buf = getattr(app, "current_buffer", None)
        text = getattr(buf, "text", "")
    except Exception:
        return False
    if not text or not text.strip():
        return False
    try:
        chat_manager._pending_prompt_restore_text = text
    except Exception:
        return False
    try:
        buf.text = ""
        if hasattr(buf, "cursor_position"):
            buf.cursor_position = 0
    except Exception:
        pass
    try:
        app.invalidate()
    except Exception:
        pass
    return True


def _create_swarm_poll_task(chat_manager, poll_interval=0.1):
    """Return an async coroutine that polls for swarm work and exits the app.

    Must be scheduled inside the running prompt_toolkit event loop (e.g.
    via ``pre_run`` or ``app.create_background_task()``).
    """

    async def poll() -> None:
        while True:
            if chat_manager.has_pending_swarm_work():
                try:
                    _save_current_buffer_for_restore(chat_manager)
                    get_app().exit(result=130)
                except Exception:
                    pass
                return
            await asyncio.sleep(poll_interval)

    return poll()


def create_swarm_pre_run(chat_manager, poll_interval=0.1):
    """Create a ``pre_run`` callback that launches the swarm-poll background task.

    Use as ``session.prompt(pre_run=create_swarm_pre_run(chat_manager))``.
    The task runs inside the event loop and calls ``get_app().exit(130)``
    directly, which works because the coroutine's ``asyncio.sleep``
    guarantees the selector timeout is ≤ *poll_interval*.

    Args:
        chat_manager: ChatManager instance.
        poll_interval: Seconds between polls (default 100ms).

    Returns:
        Callable suitable for ``pre_run``.
    """

    def pre_run():
        app = get_app()
        app.create_background_task(_create_swarm_poll_task(chat_manager, poll_interval))

    return pre_run


def create_swarm_inputhook(chat_manager, poll_interval=0.05):
    """Create a fallback inputhook that returns early when swarm work is pending.

    In prompt_toolkit ≥ 3.x the inputhook alone cannot force ``prompt()``
    to return because the underlying selector thread blocks on
    ``select(timeout=None)``.  Use ``create_swarm_pre_run`` as the
    primary mechanism; this inputhook is a belt-and-suspenders fallback.

    Args:
        chat_manager: ChatManager instance.
        poll_interval: Seconds between checks (default 50ms).

    Returns:
        Callable suitable for prompt_toolkit's ``inputhook`` parameter.
    """

    def inputhook(context):
        while not context.input_is_ready():
            if chat_manager.has_pending_swarm_work():
                return
            time.sleep(poll_interval)

    return inputhook


def _run_application_interruptible(application, chat_manager, poll_interval=0.1):
    """Run a prompt_toolkit Application synchronously with admin interrupt polling.

    Uses ``Application.run(inputhook=...)`` so the polling callback
    executes inside prompt_toolkit's own event loop (same thread),
    avoiding the ``asyncio.run()`` / cross-thread ``exit()`` hazards.

    Args:
        application: A prompt_toolkit ``Application`` instance.
        chat_manager: ChatManager (may be None — runs normally).
        poll_interval: Seconds between checks (default 100ms).

    Returns:
        The application's normal result, or ``130`` if interrupted by
        pending swarm work.
    """
    if chat_manager is None:
        return application.run()

    def _interrupt_inputhook(context):
        while True:
            if chat_manager.has_pending_swarm_work():
                try:
                    _save_current_buffer_for_restore(chat_manager)
                    get_app().exit(result=130)
                except Exception:
                    pass
                return
            if context.input_is_ready():
                return
            time.sleep(poll_interval)

    return application.run(inputhook=_interrupt_inputhook)
