"""vmCode - AI-powered coding assistant."""

import json
from pathlib import Path

try:
    _pkg_path = Path(__file__).resolve().parent.parent / "package.json"
    with open(_pkg_path) as _f:
        __version__ = json.load(_f)["version"]
except Exception:
    __version__ = "?.?.?"
