"""Centralized configuration for bone-agent."""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Set

# Load config from llm.config
# Note: src/ is added to sys.path in main.py, so we can import directly
from llm.config import _CONFIG

# Styles and themes
from pygments.styles.monokai import MonokaiStyle


class MonokaiDarkBGStyle(MonokaiStyle):
    """Monokai style with dark background for code highlighting."""
    background_color = "#141414"


_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)


def token_limit(value, default: Optional[int]) -> Optional[int]:
    """Parse a config token limit: integer tokens or off/disabled."""
    if value is None:
        return default
    if isinstance(value, bool):
        return None if value is False else default
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"off", "disabled", "disable", "none", "false", "no", "true", "0"}:
            return None
        if not normalized:
            return None
        return int(normalized.replace(",", "").replace("_", ""))
    try:
        result = int(value)
        return result if result > 0 else None
    except (ValueError, TypeError):
        return default
def _formattoken_limit(value: Optional[int]) -> str:
    return "off" if value is None else f"{value:,}"


def left_align_headings(text: str) -> str:
    """Strip markdown heading markers to avoid Rich's centering."""
    return _HEADING_RE.sub(lambda m: m.group(2), text)


@dataclass
class ToolSettings:
    """Tool execution limits and defaults."""
    max_tool_calls: int = field(default_factory=lambda: _CONFIG.get("TOOL_SETTINGS", {}).get("max_tool_calls", 100))
    command_timeout_sec: int = field(default_factory=lambda: _CONFIG.get("TOOL_SETTINGS", {}).get("command_timeout_sec", 30))
    max_command_output_lines: int = field(default_factory=lambda: _CONFIG.get("TOOL_SETTINGS", {}).get("max_command_output_lines", 100))
    max_shell_output_lines: int = field(default_factory=lambda: _CONFIG.get("TOOL_SETTINGS", {}).get("max_shell_output_lines", 200))
    max_file_preview_lines: int = field(default_factory=lambda: _CONFIG.get("TOOL_SETTINGS", {}).get("max_file_preview_lines", 200))
    disabled_tools: list = field(default_factory=lambda: _CONFIG.get("TOOL_SETTINGS", {}).get("disabled_tools", []))
    hidden_skills: list = field(default_factory=lambda: _CONFIG.get("TOOL_SETTINGS", {}).get("hidden_skills", []))

@dataclass
class FileSettings:
    """File scanning and reading limits."""
    max_file_bytes: int = field(default_factory=lambda: _CONFIG.get("FILE_SETTINGS", {}).get("max_file_bytes", 200_000))
    max_total_bytes: int = field(default_factory=lambda: _CONFIG.get("FILE_SETTINGS", {}).get("max_total_bytes", 1_500_000))
    exclude_dirs: Set[str] = None

    def __post_init__(self):
        if self.exclude_dirs is None:
            config_exclude = _CONFIG.get("FILE_SETTINGS", {}).get("exclude_dirs")
            if config_exclude:
                self.exclude_dirs = set(config_exclude)
            else:
                self.exclude_dirs = {".git", ".venv", "llama.cpp", "bin", "__pycache__"}


def _resolve_tool_compaction_limit() -> Optional[int]:
    """Resolve tool compaction token limit from config."""
    tc_cfg = _CONFIG.get("CONTEXT_SETTINGS", {}).get("tool_compaction", {})
    raw = tc_cfg.get("limit_tokens", 40_000)
    return token_limit(raw, 40_000)


@dataclass
class ToolCompactionSettings:
    """Tool result compaction settings."""
    limit_tokens: Optional[int] = field(default_factory=_resolve_tool_compaction_limit)
    min_tool_blocks: int = field(default_factory=lambda: _CONFIG.get("CONTEXT_SETTINGS", {}).get("tool_compaction", {}).get("min_tool_blocks", 5))
    compact_failed_tools: bool = field(default_factory=lambda: _CONFIG.get("CONTEXT_SETTINGS", {}).get("tool_compaction", {}).get("compact_failed_tools", True))


@dataclass
class SubAgentSettings:
    """Sub-agent token limits and behavior configuration."""
    hard_limit_tokens: Optional[int] = field(default_factory=lambda: token_limit(_CONFIG.get("SUB_AGENT_SETTINGS", {}).get("hard_limit_tokens", 150_000), 150_000))
    billed_token_limit: Optional[int] = field(default_factory=lambda: token_limit(_CONFIG.get("SUB_AGENT_SETTINGS", {}).get("billed_token_limit", 500_000), 500_000))
    enable_compaction: bool = field(default_factory=lambda: _CONFIG.get("SUB_AGENT_SETTINGS", {}).get("enable_compaction", True))
    compact_trigger_tokens: int = field(default_factory=lambda: _CONFIG.get("SUB_AGENT_SETTINGS", {}).get("compact_trigger_tokens", 50_000))
    allowed_tools: list = field(default_factory=lambda: _CONFIG.get("SUB_AGENT_SETTINGS", {}).get("allowed_tools", ["rg", "read_file", "list_directory", "web_search"]))


# Context compaction settings
@dataclass
class ContextSettings:
    """Context compaction thresholds and defaults."""
    compact_trigger_tokens: Optional[int] = field(default_factory=lambda: token_limit(_CONFIG.get("CONTEXT_SETTINGS", {}).get("compact_trigger_tokens", 100_000), 100_000))
    max_context_window: int = field(default_factory=lambda: _CONFIG.get("CONTEXT_SETTINGS", {}).get("max_context_window", 200_000))
    log_conversations: bool = field(default_factory=lambda: _CONFIG.get("CONTEXT_SETTINGS", {}).get("log_conversations", False))
    conversations_dir: str = field(default_factory=lambda: _CONFIG.get("CONTEXT_SETTINGS", {}).get("conversations_dir", "conversations"))
    notify_auto_compaction: bool = field(default_factory=lambda: _CONFIG.get("CONTEXT_SETTINGS", {}).get("notify_auto_compaction", True))
    tool_compaction: ToolCompactionSettings = field(default_factory=ToolCompactionSettings)
    hard_limit_tokens: Optional[int] = field(init=False, repr=False)

    def __post_init__(self):
        _ctx = _CONFIG.get("CONTEXT_SETTINGS", {})
        if "hard_limit_tokens" in _ctx:
            self.hard_limit_tokens = token_limit(_ctx["hard_limit_tokens"], None)
        else:
            self.hard_limit_tokens = int(self.max_context_window * 0.9)

    def format_limit(self, value: Optional[int]) -> str:
        return _formattoken_limit(value)


@dataclass
class DreamSettings:
    """Dream memory consolidation settings."""
    enabled: bool = field(default_factory=lambda: _CONFIG.get("DREAM_SETTINGS", {}).get("enabled", True))


@dataclass
class ObsidianSettings:
    """Obsidian vault integration settings.

    Supports runtime updates via update() method for /obsidian commands.
    """
    vault_path: str = field(default_factory=lambda: _CONFIG.get("OBSIDIAN_SETTINGS", {}).get("vault_path", ""))
    enabled: bool = field(default_factory=lambda: _CONFIG.get("OBSIDIAN_SETTINGS", {}).get("enabled", False))
    exclude_folders: str = field(default_factory=lambda: _CONFIG.get("OBSIDIAN_SETTINGS", {}).get("exclude_folders", ".obsidian,.trash,node_modules,.git,__pycache__"))
    project_base: str = field(default_factory=lambda: _CONFIG.get("OBSIDIAN_SETTINGS", {}).get("project_base", "Dev"))

    def update(self, **kwargs):
        """Update settings fields at runtime.

        Args:
            **kwargs: Field names and values to update
        """
        from dataclasses import fields
        valid_keys = {f.name for f in fields(self)}
        for key, value in kwargs.items():
            if key in valid_keys:
                setattr(self, key, value)

    def is_configured(self) -> bool:
        """Check if Obsidian integration is configured in settings.

        Returns:
            True if enabled and vault_path is set (does NOT validate disk)
        """
        return self.enabled and bool(self.vault_path)

    def is_active(self) -> bool:
        """Check if Obsidian integration is fully operational.

        Validates the vault path exists on disk and contains .obsidian/.

        Returns:
            True if enabled, vault_path is set, and vault is valid on disk
        """
        if not self.enabled or not self.vault_path:
            return False
        root = Path(self.vault_path).resolve()
        if not root.is_dir():
            return False
        return (root / ".obsidian").is_dir()

    @property
    def exclude_folders_list(self) -> list:
        """Return exclude_folders as a pre-parsed list of strings.

        Avoids repeated str.split(",") on every rg call.
        """
        return [f.strip() for f in self.exclude_folders.split(",") if f.strip()]


@dataclass
class SwarmSettings:
    """Swarm pool configuration.

    Swarm is inactive by default — nothing starts unless explicitly
    invoked via /swarm or --worker flags.
    """
    host: str = field(default_factory=lambda: _CONFIG.get("SWARM_SETTINGS", {}).get("host", "127.0.0.1"))
    port: int = field(default_factory=lambda: _CONFIG.get("SWARM_SETTINGS", {}).get("port", 8765))
    max_workers: int = field(default_factory=lambda: _CONFIG.get("SWARM_SETTINGS", {}).get("max_workers", 10))
    worker_tools: list = field(default_factory=lambda: _CONFIG.get("SWARM_SETTINGS", {}).get("worker_tools", [
        "rg", "read_file", "list_directory", "edit_file",
        "create_file", "execute_command",
        "create_task_list", "complete_task", "show_task_list",
    ]))


# Global instances
tool_settings = ToolSettings()
context_settings = ContextSettings()
sub_agent_settings = SubAgentSettings()
dream_settings = DreamSettings()
obsidian_settings = ObsidianSettings()
swarm_settings = SwarmSettings()
# Tool execution constants
MAX_TOOL_CALLS = tool_settings.max_tool_calls
MAX_COMMAND_OUTPUT_LINES = tool_settings.max_command_output_lines
MAX_SHELL_OUTPUT_LINES = tool_settings.max_shell_output_lines
MAX_FILE_PREVIEW_LINES = tool_settings.max_file_preview_lines
