"""Shared path constants for vmCode.

Centralizes APP_ROOT, REPO_ROOT, and tool paths (e.g. ripgrep)
so they can be imported from both core and ui modules without
creating circular dependencies.
"""

import os
import sys
from pathlib import Path


def _resolve_app_root() -> Path:
    """Return the application root directory.

    For frozen builds (PyInstaller), this is the directory containing
    the executable. For source installs, it's two levels up from this file
    (i.e. the repo root).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


APP_ROOT = _resolve_app_root()
REPO_ROOT = Path.cwd().resolve()

# Platform-agnostic ripgrep path: 'rg' on Unix/Linux, 'rg.exe' on Windows
_RG_EXE_NAME = "rg.exe" if os.name == "nt" else "rg"
RG_EXE_PATH = (APP_ROOT / "bin" / _RG_EXE_NAME).resolve()
