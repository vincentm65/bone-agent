"""Centralized configuration for bone-agent."""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Set

# Load config from llm.config
# Note: src/ is added to sys.path in main.py, so we can import directly
from llm.config import _CONFIG

# Styles and themes
from pygments.styles.monokai import MonokaiStyle


class MonokaiDarkBGStyle(MonokaiStyle):
    """Monokai style with dark background for code highlighting."""
    background_color = "#141414"


_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)


def left_align_headings(text: str) -> str:
    """Strip markdown heading markers to avoid Rich's centering."""
    return _HEADING_RE.sub(lambda m: m.group(2), text)


@dataclass
class ServerSettings:
    """Local llama-server configuration."""
    ngl_layers: int = field(default_factory=lambda: _CONFIG.get("SERVER_SETTINGS", {}).get("ngl_layers", 30))
    ctx_size: int = field(default_factory=lambda: _CONFIG.get("SERVER_SETTINGS", {}).get("ctx_size", 8192))
    n_predict: int = field(default_factory=lambda: _CONFIG.get("SERVER_SETTINGS", {}).get("n_predict", 8192))
    rope_scale: float = field(default_factory=lambda: _CONFIG.get("SERVER_SETTINGS", {}).get("rope_scale", 1.0))
    health_check_timeout_sec: int = field(default_factory=lambda: _CONFIG.get("SERVER_SETTINGS", {}).get("health_check_timeout_sec", 120))
    health_check_interval_sec: float = field(default_factory=lambda: _CONFIG.get("SERVER_SETTINGS", {}).get("health_check_interval_sec", 1.0))


@dataclass
class ToolSettings:
    """Tool execution limits and defaults."""
    max_tool_calls: int = field(default_factory=lambda: _CONFIG.get("TOOL_SETTINGS", {}).get("max_tool_calls", 100))
    command_timeout_sec: int = field(default_factory=lambda: _CONFIG.get("TOOL_SETTINGS", {}).get("command_timeout_sec", 30))
    enable_parallel_execution: bool = field(default_factory=lambda: _CONFIG.get("TOOL_SETTINGS", {}).get("enable_parallel_execution", True))
    max_parallel_workers: int = field(default_factory=lambda: _CONFIG.get("TOOL_SETTINGS", {}).get("max_parallel_workers", 10))
    max_command_output_lines: int = field(default_factory=lambda: _CONFIG.get("TOOL_SETTINGS", {}).get("max_command_output_lines", 100))
    max_shell_output_lines: int = field(default_factory=lambda: _CONFIG.get("TOOL_SETTINGS", {}).get("max_shell_output_lines", 200))
    max_file_preview_lines: int = field(default_factory=lambda: _CONFIG.get("TOOL_SETTINGS", {}).get("max_file_preview_lines", 200))
    disabled_tools: list = field(default_factory=lambda: _CONFIG.get("TOOL_SETTINGS", {}).get("disabled_tools", []))

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


@dataclass
class ToolCompactionSettings:
    """Per-message tool result compaction settings."""
    enable_per_message_compaction: bool = field(default_factory=lambda: _CONFIG.get("CONTEXT_SETTINGS", {}).get("tool_compaction", {}).get("enable_per_message_compaction", True))
    keep_recent_tool_blocks: int = field(default_factory=lambda: _CONFIG.get("CONTEXT_SETTINGS", {}).get("tool_compaction", {}).get("keep_recent_tool_blocks", 3))
    compact_failed_tools: bool = field(default_factory=lambda: _CONFIG.get("CONTEXT_SETTINGS", {}).get("tool_compaction", {}).get("compact_failed_tools", True))


@dataclass
class SubAgentSettings:
    """Sub-agent token limits and behavior configuration."""
    soft_limit_tokens: int = field(default_factory=lambda: _CONFIG.get("SUB_AGENT_SETTINGS", {}).get("soft_limit_tokens", 75_000))
    hard_limit_tokens: int = field(default_factory=lambda: _CONFIG.get("SUB_AGENT_SETTINGS", {}).get("hard_limit_tokens", 300_000))
    enable_compaction: bool = field(default_factory=lambda: _CONFIG.get("SUB_AGENT_SETTINGS", {}).get("enable_compaction", True))
    compact_trigger_tokens: int = field(default_factory=lambda: _CONFIG.get("SUB_AGENT_SETTINGS", {}).get("compact_trigger_tokens", 50_000))
    allowed_tools: list = field(default_factory=lambda: _CONFIG.get("SUB_AGENT_SETTINGS", {}).get("allowed_tools", ["rg", "read_file", "list_directory", "web_search"]))


# Context compaction settings
@dataclass
class ContextSettings:
    """Context compaction thresholds and defaults."""
    compact_trigger_tokens: int = field(default_factory=lambda: _CONFIG.get("CONTEXT_SETTINGS", {}).get("compact_trigger_tokens", 100_000))
    max_context_window: int = field(default_factory=lambda: _CONFIG.get("CONTEXT_SETTINGS", {}).get("max_context_window", 200_000))
    log_conversations: bool = field(default_factory=lambda: _CONFIG.get("CONTEXT_SETTINGS", {}).get("log_conversations", False))
    conversations_dir: str = field(default_factory=lambda: _CONFIG.get("CONTEXT_SETTINGS", {}).get("conversations_dir", "conversations"))
    notify_auto_compaction: bool = field(default_factory=lambda: _CONFIG.get("CONTEXT_SETTINGS", {}).get("notify_auto_compaction", True))
    tool_compaction: ToolCompactionSettings = field(default_factory=ToolCompactionSettings)
    hard_limit_tokens: int = field(init=False, repr=False)

    def __post_init__(self):
        _ctx = _CONFIG.get("CONTEXT_SETTINGS", {})
        if "hard_limit_tokens" in _ctx:
            self.hard_limit_tokens = _ctx["hard_limit_tokens"]
        else:
            self.hard_limit_tokens = int(self.max_context_window * 0.9)


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


# Global instances
server_settings = ServerSettings()
tool_settings = ToolSettings()
file_settings = FileSettings()
context_settings = ContextSettings()
sub_agent_settings = SubAgentSettings()
obsidian_settings = ObsidianSettings()

# Tool execution constants
MAX_TOOL_CALLS = tool_settings.max_tool_calls
MAX_COMMAND_OUTPUT_LINES = tool_settings.max_command_output_lines
MAX_SHELL_OUTPUT_LINES = tool_settings.max_shell_output_lines
MAX_FILE_PREVIEW_LINES = tool_settings.max_file_preview_lines
