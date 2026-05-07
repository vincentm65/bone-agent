"""Utilities for removing terminal control sequences from persisted text."""

from __future__ import annotations

import re


# ANSI/VT control sequences commonly emitted by prompt_toolkit redraws:
# CSI (ESC [ ... final), OSC (ESC ] ... BEL/ST), plus a small set of
# single-character ESC controls. Keep normal tabs/newlines/carriage returns.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_SINGLE_RE = re.compile(r"\x1b[@-Z\\-_]")
_C0_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_terminal_control(text: str) -> str:
    """Return *text* without terminal control/redraw sequences.

    This is intended for user/model context and conversation logs, not for
    live terminal rendering. It removes prompt_toolkit/Rich escape sequences
    that can otherwise become persistent chat history artifacts.
    """
    if not text:
        return text

    text = _ANSI_OSC_RE.sub("", text)
    text = _ANSI_CSI_RE.sub("", text)
    text = _ANSI_SINGLE_RE.sub("", text)
    text = _C0_CONTROL_RE.sub("", text)
    return text


def sanitize_message_content(content):
    """Sanitize text inside provider-neutral message content."""
    if isinstance(content, str):
        return strip_terminal_control(content)
    if not isinstance(content, list):
        return content

    sanitized = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            updated = dict(block)
            updated["text"] = strip_terminal_control(str(updated.get("text", "")))
            sanitized.append(updated)
        else:
            sanitized.append(block)
    return sanitized


def sanitize_message(message):
    """Return a message dict with sanitized text content."""
    if not isinstance(message, dict):
        return message
    updated = dict(message)
    if "content" in updated:
        updated["content"] = sanitize_message_content(updated.get("content"))
    return updated


class SanitizedMessageList(list):
    """List that strips terminal controls from message content on mutation."""

    def __init__(self, iterable=()):
        super().__init__(sanitize_message(item) for item in iterable)

    def append(self, item):
        super().append(sanitize_message(item))

    def extend(self, iterable):
        super().extend(sanitize_message(item) for item in iterable)

    def insert(self, index, item):
        super().insert(index, sanitize_message(item))

    def __setitem__(self, index, value):
        if isinstance(index, slice):
            value = [sanitize_message(item) for item in value]
        else:
            value = sanitize_message(value)
        super().__setitem__(index, value)
