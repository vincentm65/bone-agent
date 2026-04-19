import os
import platform
from pathlib import Path
import yaml

# Provider selection - loaded from config (see after PROVIDER_REGISTRY definition)

def resolve_config_path() -> Path:
    """Resolve config.yaml path.

    Resolution order:
    1. VMCODE_CONFIG_PATH — explicit user config path (set by npm wrapper)
    2. ~/.vmcode/config.yaml — user home dotfile (dev and npm installs)
    """
    _user_cfg = os.environ.get('VMCODE_CONFIG_PATH')
    if _user_cfg:
        return Path(_user_cfg).resolve()
    return Path.home() / '.vmcode' / 'config.yaml'

# Module-level config path (single source of truth)
CONFIG_PATH = resolve_config_path()

# Environment variable names for API keys (env vars take precedence over config file)
ENV_API_KEYS = {
    'ANTHROPIC_API_KEY': os.environ.get('ANTHROPIC_API_KEY'),
    'OPENAI_API_KEY': os.environ.get('OPENAI_API_KEY'),
    'GLM_PLAN_API_KEY': os.environ.get('GLM_PLAN_API_KEY'),
    'GLM_API_KEY': os.environ.get('GLM_API_KEY'),
    'GEMINI_API_KEY': os.environ.get('GEMINI_API_KEY'),
    'OPENROUTER_API_KEY': os.environ.get('OPENROUTER_API_KEY'),
    'KIMI_API_KEY': os.environ.get('KIMI_API_KEY'),
    'MINIMAX_PLAN_API_KEY': os.environ.get('MINIMAX_PLAN_API_KEY'),
    'MINIMAX_API_KEY': os.environ.get('MINIMAX_API_KEY'),
    'VMCODE_PROXY_API_KEY': os.environ.get('VMCODE_PROXY_API_KEY'),
}

# Detect platform for llama.cpp paths
_IS_WINDOWS = platform.system() == "Windows"
_IS_LINUX = platform.system() == "Linux"

# Set llama.cpp paths based on platform
if _IS_WINDOWS:
    _LLAMA_SERVER_NAME = "llama-server.exe"
    _LLAMA_BUILD_DIR = "build"
elif _IS_LINUX:
    _LLAMA_SERVER_NAME = "llama-server"
    _LLAMA_BUILD_DIR = "build-linux"
else:
    # Fallback for macOS or other platforms
    _LLAMA_SERVER_NAME = "llama-server"
    _LLAMA_BUILD_DIR = "build"

def _load_config():
    """Load config from YAML file, with environment variable overrides for API keys.
    
    Environment variables take precedence over values in config.yaml.
    """
    config_path = resolve_config_path()
    if not config_path.exists():
        return {}
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
    except yaml.YAMLError:
        config = {}
    
    # Override API keys from environment variables (env vars take precedence)
    for key, env_value in ENV_API_KEYS.items():
        if env_value:  # Only override if env var is set and non-empty
            config[key] = env_value
    
    return config


_CONFIG = _load_config()

# Cache for provider registry (built once at module load)
_provider_registry_cache = None
_cached_provider = None


def get_model_cost(model_name: str, config: dict | None = None) -> tuple[float, float]:
    """Get model-specific (cost_in, cost_out) per 1M tokens from MODEL_PRICES.

    Shared utility — used by both the provider registry builder and config_manager.
    Returns (0.0, 0.0) for unknown models.

    Args:
        model_name: Model name to look up.
        config: Optional config dict (defaults to _CONFIG). Pass a fresh config
                after runtime edits to get up-to-date pricing.
    """
    model_prices = (config if config is not None else _CONFIG).get("MODEL_PRICES", {})
    if model_name in model_prices:
        mc = model_prices[model_name]
        return float(mc.get("cost_in", 0.0)), float(mc.get("cost_out", 0.0))
    return 0.0, 0.0


def _model_cost(model_config_key):
    """Return {cost_in, cost_out} dict for use in provider registry via **spread.

    Reads from module-level _CONFIG (not an injected config). Safe because
    reload_config() invalidates the provider registry cache, forcing a rebuild.
    """
    ci, co = get_model_cost(_CONFIG.get(model_config_key, ""))
    return {"cost_in": ci, "cost_out": co}


def _get_provider_registry():
    """Build PROVIDER_REGISTRY from current config (cached)."""
    global _provider_registry_cache
    if _provider_registry_cache is not None:
        return _provider_registry_cache

    _provider_registry_cache = {
        "local": {
            "type": "local",
            "api_key": None,
            "model": _CONFIG.get("LOCAL_MODEL_PATH", ""),
            "api_model": "model",
            "api_base": "http://127.0.0.1:8080",
            "endpoint": "/v1/chat/completions",
            "error_prefix": "local server",
            "config_keys": {
                "LOCAL_MODEL_PATH": "",
                "LOCAL_SERVER_PATH": str(
                    Path(__file__).resolve().parents[2] /
                    f"llama.cpp/{_LLAMA_BUILD_DIR}/bin/{_LLAMA_SERVER_NAME}"
                ),
            },
            "extra": {
                "host": "127.0.0.1",
                "port": 8080,
            },
            "default_temperature": 0.1,
            "default_top_p": 0.9,
            "allow_top_p": True,
            "allow_temperature": True,
            "cost_in": 0.0,
            "cost_out": 0.0
        },
        "openrouter": {
            "type": "api",
            "api_key": _CONFIG.get("OPENROUTER_API_KEY", ""),
            "model": _CONFIG.get("OPENROUTER_MODEL", ""),
            "api_base": _CONFIG.get("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1"),
            "endpoint": "/chat/completions",
            "error_prefix": "OpenRouter",
            "headers_extra": {
                "HTTP-Referer": "http://localhost:8080",
                "X-Title": "Chat App"
            },
            "config_keys": {
                "OPENROUTER_API_KEY": "",
                "OPENROUTER_MODEL": "",
                "OPENROUTER_API_BASE": "https://openrouter.ai/api/v1",
            },
            "default_temperature": 0.1,
            "default_top_p": 0.9,
            "allow_top_p": True,
            "allow_temperature": True,
            **_model_cost("OPENROUTER_MODEL"),
        },
        "glm_plan": {
            "type": "api",
            "api_key": _CONFIG.get("GLM_PLAN_API_KEY", ""),
            "model": _CONFIG.get("GLM_PLAN_MODEL", ""),
            "api_base": _CONFIG.get("GLM_PLAN_API_BASE", "https://open.bigmodel.cn/api/paas/v4"),
            "endpoint": "/chat/completions",
            "error_prefix": "GLM",
            "config_keys": {
                "GLM_PLAN_API_KEY": "",
                "GLM_PLAN_MODEL": "",
                "GLM_PLAN_API_BASE": "https://open.bigmodel.cn/api/paas/v4",
            },
            "default_temperature": 0.1,
            "default_top_p": 0.9,
            "allow_top_p": True,
            "allow_temperature": True,
            **_model_cost("GLM_PLAN_MODEL"),
        },
        "glm": {
            "type": "api",
            "api_key": _CONFIG.get("GLM_API_KEY", ""),
            "model": _CONFIG.get("GLM_MODEL", ""),
            "api_base": _CONFIG.get("GLM_API_BASE", "https://open.bigmodel.cn/api/paas/v4"),
            "endpoint": "/chat/completions",
            "error_prefix": "GLM",
            "config_keys": {
                "GLM_API_KEY": "",
                "GLM_MODEL": "",
                "GLM_API_BASE": "https://open.bigmodel.cn/api/paas/v4",
            },
            "default_temperature": 0.1,
            "default_top_p": 0.9,
            "allow_top_p": True,
            "allow_temperature": True,
            **_model_cost("GLM_MODEL"),
        },
        "openai": {
            "type": "api",
            "api_key": _CONFIG.get("OPENAI_API_KEY", ""),
            "model": _CONFIG.get("OPENAI_MODEL", ""),
            "api_base": _CONFIG.get("OPENAI_API_BASE", "https://api.openai.com/v1"),
            "endpoint": "/chat/completions",
            "error_prefix": "OpenAI",
            "config_keys": {
                "OPENAI_API_KEY": "",
                "OPENAI_MODEL": "",
                "OPENAI_API_BASE": "https://api.openai.com/v1",
            },
            "default_temperature": 0.1,
            "default_top_p": 0.9,
            "allow_top_p": False,
            "allow_temperature": False,
            **_model_cost("OPENAI_MODEL"),
        },
        "gemini": {
            "type": "api",
            "api_key": _CONFIG.get("GEMINI_API_KEY", ""),
            "model": _CONFIG.get("GEMINI_MODEL", ""),
            "api_base": _CONFIG.get("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta"),
            "endpoint": "/chat/completions",
            "error_prefix": "Gemini",
            "config_keys": {
                "GEMINI_API_KEY": "",
                "GEMINI_MODEL": "",
                "GEMINI_API_BASE": "https://generativelanguage.googleapis.com/v1beta",
            },
            "default_temperature": 0.1,
            "default_top_p": 0.9,
            "allow_top_p": True,
            "allow_temperature": True,
            **_model_cost("GEMINI_MODEL"),
        },
        "minimax_plan": {
            "type": "api",
            "api_key": _CONFIG.get("MINIMAX_PLAN_API_KEY", ""),
            "model": _CONFIG.get("MINIMAX_PLAN_MODEL", ""),
            "api_base": _CONFIG.get("MINIMAX_PLAN_API_BASE", "https://api.minimax.io/anthropic/v1"),
            "endpoint": "/messages",
            "error_prefix": "MiniMax",
            "config_keys": {
                "MINIMAX_PLAN_API_KEY": "",
                "MINIMAX_PLAN_MODEL": "",
                "MINIMAX_PLAN_API_BASE": "https://api.minimax.io/anthropic/v1",
            },
            "default_temperature": 0.1,
            "default_top_p": 0.9,
            "allow_top_p": False,
            "allow_temperature": True,
            "max_tokens": 4096,
            **_model_cost("MINIMAX_PLAN_MODEL"),
        },
        "minimax": {
            "type": "api",
            "api_key": _CONFIG.get("MINIMAX_API_KEY", ""),
            "model": _CONFIG.get("MINIMAX_MODEL", ""),
            "api_base": _CONFIG.get("MINIMAX_API_BASE", "https://api.minimax.io/anthropic/v1"),
            "endpoint": "/messages",
            "error_prefix": "MiniMax",
            "config_keys": {
                "MINIMAX_API_KEY": "",
                "MINIMAX_MODEL": "",
                "MINIMAX_API_BASE": "https://api.minimax.io/anthropic/v1",
            },
            "default_temperature": 0.1,
            "default_top_p": 0.9,
            "allow_top_p": False,
            "allow_temperature": True,
            "max_tokens": 4096,
            **_model_cost("MINIMAX_MODEL"),
        },
        "anthropic": {
            "type": "api",
            "api_key": _CONFIG.get("ANTHROPIC_API_KEY", ""),
            "model": _CONFIG.get("ANTHROPIC_MODEL", ""),
            "api_base": _CONFIG.get("ANTHROPIC_API_BASE", "https://api.anthropic.com/v1"),
            "endpoint": "/messages",
            "error_prefix": "Anthropic",
            "headers_extra": {
                "anthropic-version": "2023-06-01"
            },
            "config_keys": {
                "ANTHROPIC_API_KEY": "",
                "ANTHROPIC_MODEL": "",
                "ANTHROPIC_API_BASE": "https://api.anthropic.com/v1",
            },
            "default_temperature": 0.1,
            "default_top_p": 0.9,
            "allow_top_p": False,
            "allow_temperature": True,
            "max_tokens": 4096,
            **_model_cost("ANTHROPIC_MODEL"),
        },
        "kimi": {
            "type": "api",
            "api_key": _CONFIG.get("KIMI_API_KEY", ""),
            "model": _CONFIG.get("KIMI_MODEL", ""),
            "api_base": _CONFIG.get("KIMI_API_BASE", "https://api.moonshot.cn/v1"),
            "endpoint": "/chat/completions",
            "error_prefix": "Kimi",
            "config_keys": {
                "KIMI_API_KEY": "",
                "KIMI_MODEL": "",
                "KIMI_API_BASE": "https://api.moonshot.cn/v1",
            },
            "default_temperature": 0.1,
            "default_top_p": 0.9,
            "allow_top_p": True,
            "allow_temperature": True,
            **_model_cost("KIMI_MODEL"),
        },
        "vmcode": {
            "type": "api",
            "api_key": _CONFIG.get("VMCODE_PROXY_API_KEY", ""),
            "model": _CONFIG.get("VMCODE_PROXY_MODEL", "openai/gpt-4o-mini"),
            "api_base": _CONFIG.get("VMCODE_PROXY_API_BASE", "https://api.vmcode.dev"),
            "endpoint": "/v1/chat/completions",
            "error_prefix": "vmCode Proxy",
            "config_keys": {
                "VMCODE_PROXY_API_KEY": "",
                "VMCODE_PROXY_MODEL": "openai/gpt-4o-mini",
                "VMCODE_PROXY_API_BASE": "https://api.vmcode.dev",
            },
            "default_temperature": 0.1,
            "default_top_p": 0.9,
            "allow_top_p": True,
            "allow_temperature": True,
            **_model_cost("VMCODE_PROXY_MODEL"),
        },
    }
    return _provider_registry_cache


def _get_provider():
    """Get the current provider from config (cached)."""
    global _cached_provider
    if _cached_provider is not None:
        return _cached_provider

    last_provider = _CONFIG.get("LAST_PROVIDER")
    if last_provider and last_provider in _provider_registry_cache:
        _cached_provider = last_provider
        return _cached_provider
    _cached_provider = "glm_plan"
    return _cached_provider


def reload_config():
    """Reload config from disk and invalidate caches.
    
    Reloads both the config.yaml file and environment variables.
    
    Note: This is a manual operation - call after config changes.
    """
    global _CONFIG, _provider_registry_cache, _cached_provider, PROVIDER_REGISTRY, LLM_PROVIDER, STATUS_BAR_SETTINGS
    _CONFIG = _load_config()
    _provider_registry_cache = None
    _cached_provider = None
    # Rebuild module-level variables
    PROVIDER_REGISTRY = _get_provider_registry()
    LLM_PROVIDER = _get_provider()
    # Rebuild status bar settings
    STATUS_BAR_SETTINGS = _build_status_bar_settings()


def _build_status_bar_settings():
    """Build STATUS_BAR_SETTINGS dict from current _CONFIG."""
    sbs = _CONFIG.get("STATUS_BAR_SETTINGS", {})
    return {
        "show_curr_tokens": sbs.get("show_curr_tokens", True),
        "show_in_tokens": sbs.get("show_in_tokens", True),
        "show_out_tokens": sbs.get("show_out_tokens", True),
        "show_total_tokens": sbs.get("show_total_tokens", True),
        "show_cost": sbs.get("show_cost", True),
    }


def update_status_bar_settings(settings_dict):
    """Update STATUS_BAR_SETTINGS at runtime and persist to config.

    Args:
        settings_dict: Dict of settings to update (e.g., {"show_cost": False})

    Returns:
        Updated STATUS_BAR_SETTINGS dict
    """
    global STATUS_BAR_SETTINGS
    STATUS_BAR_SETTINGS.update(settings_dict)
    return STATUS_BAR_SETTINGS


def get_providers():
    """Get list of available providers.

    Returns:
        list: List of provider names from PROVIDER_REGISTRY.
    """
    return list(PROVIDER_REGISTRY.keys())


# ============================================================================
# PROVIDER REGISTRY - Centralized provider configuration
# ============================================================================

# Build provider registry and export as module-level constants (loaded once)
PROVIDER_REGISTRY = _get_provider_registry()
LLM_PROVIDER = _get_provider()


__all__ = [
    "CONFIG_PATH",
    "PROVIDER_REGISTRY",
    "get_providers",
    "get_model_cost",
    "LLM_PROVIDER",
    "TOOLS_ENABLED",
    "TOOLS_REQUIRE_CONFIRMATION",
    "WEB_SEARCH_REQUIRE_CONFIRMATION",
    "APPROVE_MODES",
    "APPROVE_MODE_LABELS",
    "INTERACTION_MODES",
    "INTERACTION_MODE_LABELS",

    "PLAN_TYPES",
    "PLAN_TYPE_LABELS",
    "get_provider_config",
    "generate_config_template",
    "reload_config",
    "STATUS_BAR_SETTINGS",
    "update_status_bar_settings",
]


def generate_config_template():
    """Generate default template for config.json from provider registry."""
    template = {}
    for provider, config in PROVIDER_REGISTRY.items():
        if "config_keys" in config:
            template.update(config["config_keys"])
    return template

# Tooling configuration
TOOLS_ENABLED = True
TOOLS_REQUIRE_CONFIRMATION = False
WEB_SEARCH_REQUIRE_CONFIRMATION = False

# Status bar configuration
STATUS_BAR_SETTINGS = _build_status_bar_settings()

# Tool approval modes
APPROVE_MODES = ("safe", "accept_edits", "danger")
CYCLEABLE_APPROVE_MODES = ("safe", "accept_edits")
APPROVE_MODE_LABELS = {
    "safe": "Safe",
    "accept_edits": "Accept Edits",
    "danger": "Danger",
}

# Interaction modes
INTERACTION_MODES = ("edit", "plan")
INTERACTION_MODE_LABELS = {
    "edit": "Edit (Full Access)",
    "plan": "Plan (Read-Only)",
}

# Plan types (planning behavior options for Plan interaction mode)
PLAN_TYPES = ("feature", "refactor", "debug", "optimize")
PLAN_TYPE_LABELS = {
    "feature": "Feature",
    "refactor": "Refactor",
    "debug": "Debug",
    "optimize": "Optimize"
}

def get_provider_config(provider: str):
    """Retrieve the configuration dictionary for a given provider.

    Args:
        provider (str): Provider name (e.g., 'local', 'openrouter', 'glm', 'openai').

    Returns:
        dict: Provider config from the PROVIDER_REGISTRY.
    """
    return PROVIDER_REGISTRY.get(provider, {})
