"""Linux clipboard image reader for prompt image paste."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional


MAX_CLIPBOARD_IMAGE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class ClipboardImage:
    """Image data read from the clipboard."""

    data: bytes
    mime_type: str


@dataclass(frozen=True)
class ClipboardImageResult:
    """Structured clipboard image read result."""

    image: Optional[ClipboardImage] = None
    reason: Optional[str] = None
    message: str = ""


_IMAGE_MIME_TYPES = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
)


def _session_type() -> str:
    return (os.environ.get("XDG_SESSION_TYPE") or "").lower()


def _detect_wayland_mime() -> Optional[str]:
    try:
        result = subprocess.run(
            ["wl-paste", "--list-types"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0:
        return None

    types = result.stdout.decode("utf-8", errors="replace").splitlines()
    return next((mime for mime in _IMAGE_MIME_TYPES if mime in types), None)


def _read_wayland_image() -> ClipboardImageResult:
    if not shutil.which("wl-paste"):
        return ClipboardImageResult(
            reason="missing_tool",
            message="Install wl-clipboard to paste images on Wayland.",
        )

    mime_type = _detect_wayland_mime()
    if not mime_type:
        return ClipboardImageResult(reason="no_image")

    try:
        result = subprocess.run(
            ["wl-paste", "--no-newline", "--type", mime_type],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return ClipboardImageResult(reason="clipboard_error", message="Timed out reading clipboard image.")
    except OSError as exc:
        return ClipboardImageResult(reason="clipboard_error", message=str(exc))

    if result.returncode != 0:
        return ClipboardImageResult(reason="clipboard_error", message=result.stderr.decode("utf-8", errors="replace").strip())
    if not result.stdout:
        return ClipboardImageResult(reason="no_image")
    if len(result.stdout) > MAX_CLIPBOARD_IMAGE_BYTES:
        return ClipboardImageResult(reason="too_large", message="Clipboard image is larger than 10 MB.")

    return ClipboardImageResult(image=ClipboardImage(data=result.stdout, mime_type=mime_type))


def _read_x11_image() -> ClipboardImageResult:
    if not shutil.which("xclip"):
        return ClipboardImageResult(
            reason="missing_tool",
            message="Install xclip to paste images on X11.",
        )

    for mime_type in _IMAGE_MIME_TYPES:
        try:
            result = subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", mime_type, "-o"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
            )
        except subprocess.TimeoutExpired:
            return ClipboardImageResult(reason="clipboard_error", message="Timed out reading clipboard image.")
        except OSError as exc:
            return ClipboardImageResult(reason="clipboard_error", message=str(exc))

        if result.returncode == 0 and result.stdout:
            if len(result.stdout) > MAX_CLIPBOARD_IMAGE_BYTES:
                return ClipboardImageResult(reason="too_large", message="Clipboard image is larger than 10 MB.")
            return ClipboardImageResult(image=ClipboardImage(data=result.stdout, mime_type=mime_type))

    return ClipboardImageResult(reason="no_image")


def read_clipboard_image() -> ClipboardImageResult:
    """Read an image from the Linux clipboard, if one is available."""
    if os.name != "posix":
        return ClipboardImageResult(reason="unsupported_platform", message="Image paste is currently Linux-only.")

    if _session_type() == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
        return _read_wayland_image()

    return _read_x11_image()
