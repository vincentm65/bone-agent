import os
from pathlib import Path
import yaml

# Provider selection - loaded from config (see after PROVIDER_REGISTRY definition)

def resolve_config_path() -> Path:
    """Resolve config.yaml path.

    Resolution order:
    1. BONE_CONFIG_PATH — explicit user config path (set by npm wrapper)
    2. ~/.bone/config.yaml — user home dotfile (dev and npm installs)
    """
    _user_cfg = os.environ.get('BONE_CONFIG_PATH')
    if _user_cfg:
        return Path(_user_cfg).resolve()
    return Path.home() / '.bone' / 'config.yaml'

# Module-level config path (single source of truth)
CONFIG_PATH = resolve_config_path()

# Environment variable names for API keys (env vars take precedence over config file)
ENV_API_KEYS = [
    'ANTHROPIC_API_KEY',
    'OPENAI_API_KEY',
    'GLM_PLAN_API_KEY',
    'GLM_API_KEY',
    'GEMINI_API_KEY',
    'OPENROUTER_API_KEY',
    'KIMI_API_KEY',
    'MINIMAX_PLAN_API_KEY',
    'MINIMAX_API_KEY',
    'BONE_PROXY_API_KEY',
    'CODEX_PLAN_API_KEY',
    'DEEPSEEK_API_KEY',
]

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
    for key in ENV_API_KEYS:
        env_value = os.environ.get(key)
        if env_value:  # Only override if env var is set and non-empty
            config[key] = env_value
    
    return config


_CONFIG = _load_config()


def _get_codex_token() -> str:
    """Read access token from Codex CLI's cached auth (~/.codex/auth.json).

    Returns the access_token if available, empty string otherwise.
    Codex CLI stores OAuth tokens here after `codex login`.
    """
    try:
        auth_path = Path.home() / ".codex" / "auth.json"
        if not auth_path.exists():
            return ""
        import json
        data = json.loads(auth_path.read_text(encoding="utf-8"))
        return data.get("tokens", {}).get("access_token", "")
    except Exception:
        return ""


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
            "type": "api",
            "api_key": _CONFIG.get("LLM_API_KEY", ""),
            "model": _CONFIG.get("LLM_MODEL", ""),
            "api_base": _CONFIG.get("LLM_API_BASE", "http://127.0.0.1:8080"),
            "endpoint": _CONFIG.get("LLM_ENDPOINT", "/v1/chat/completions"),
            "error_prefix": "local server",
            "config_keys": {
                "LLM_API_KEY": "",
                "LLM_API_BASE": "http://127.0.0.1:8080",
                "LLM_ENDPOINT": "/v1/chat/completions",
                "LLM_MODEL": "",
            },
            "default_temperature": 0.1,
            "default_top_p": 0.9,
            "allow_top_p": False,
            "allow_temperature": False,
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
        "deepseek": {
            "type": "api",
            "api_key": _CONFIG.get("DEEPSEEK_API_KEY", ""),
            "model": _CONFIG.get("DEEPSEEK_MODEL", ""),
            "api_base": _CONFIG.get("DEEPSEEK_API_BASE", "https://api.deepseek.com"),
            "endpoint": "/chat/completions",
            "error_prefix": "DeepSeek",
            "config_keys": {
                "DEEPSEEK_API_KEY": "",
                "DEEPSEEK_MODEL": "",
                "DEEPSEEK_API_BASE": "https://api.deepseek.com",
            },
            "default_temperature": 0.1,
            "default_top_p": 0.9,
            "allow_top_p": True,
            "allow_temperature": True,
            **_model_cost("DEEPSEEK_MODEL"),
        },
        "codex": {
            "type": "api",
            "api_key": _CONFIG.get("CODEX_PLAN_API_KEY", "") or _get_codex_token(),
            "model": _CONFIG.get("CODEX_PLAN_MODEL", "gpt-5.4-mini"),
            "api_base": _CONFIG.get("CODEX_PLAN_API_BASE", "https://chatgpt.com/backend-api/codex"),
            "endpoint": "/responses",
            "error_prefix": "Codex",
            "config_keys": {
                "CODEX_PLAN_API_KEY": "",
                "CODEX_PLAN_MODEL": "",
                "CODEX_PLAN_API_BASE": "https://chatgpt.com/backend-api/codex",
            },
            "allow_temperature": False,
            "allow_top_p": False,
            **_model_cost("CODEX_PLAN_MODEL"),
        },
        "bone": {
            "type": "api",
            "provider": "bone",
            "api_key": _CONFIG.get("BONE_PROXY_API_KEY", ""),
            "model": _CONFIG.get("BONE_PROXY_MODEL", "openai/gpt-4o-mini"),
            "api_base": _CONFIG.get("BONE_PROXY_API_BASE", "https://api.vmcode.dev"),
            "endpoint": "/v1/chat/completions",
            "error_prefix": "bone-agent Proxy",
            "config_keys": {
                "BONE_PROXY_API_KEY": "",
                "BONE_PROXY_MODEL": "openai/gpt-4o-mini",
                "BONE_PROXY_API_BASE": "https://api.vmcode.dev",
            },
            "default_temperature": 0.1,
            "default_top_p": 0.9,
            "allow_top_p": True,
            "allow_temperature": True,
            **_model_cost("BONE_PROXY_MODEL"),
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
    global _CONFIG, _provider_registry_cache, _cached_provider, PROVIDER_REGISTRY, LLM_PROVIDER, STATUS_BAR_SETTINGS, MEMORY_SETTINGS
    _CONFIG = _load_config()
    _provider_registry_cache = None
    _cached_provider = None
    # Rebuild module-level variables
    PROVIDER_REGISTRY = _get_provider_registry()
    LLM_PROVIDER = _get_provider()
    # Rebuild status bar settings
    STATUS_BAR_SETTINGS = _build_status_bar_settings()
    # Rebuild memory settings
    MEMORY_SETTINGS = _build_memory_settings()


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


def _build_memory_settings():
    """Build MEMORY_SETTINGS dict from current _CONFIG."""
    ms = _CONFIG.get("MEMORY_SETTINGS", {})
    return {
        "enabled": ms.get("enabled", True),
    }


def update_memory_settings(settings_dict):
    """Update MEMORY_SETTINGS at runtime.

    Returns:
        Updated MEMORY_SETTINGS dict
    """
    global MEMORY_SETTINGS
    MEMORY_SETTINGS.update(settings_dict)
    return MEMORY_SETTINGS


def update_status_bar_settings(settings_dict):
    """Update STATUS_BAR_SETTINGS at runtime.

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


def get_provider_display_name(provider: str) -> str:
    """Return the user-facing provider name for a provider key."""
    display_names = {
        "codex": "Codex",
    }
    return display_names.get(provider, provider.replace("_", " ").title())


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
    "get_provider_display_name",
    "get_model_cost",
    "LLM_PROVIDER",
    "TOOLS_ENABLED",
    "TOOLS_REQUIRE_CONFIRMATION",
    "APPROVE_MODES",
    "APPROVE_MODE_LABELS",

    "get_provider_config",
    "generate_config_template",
    "reload_config",
    "STATUS_BAR_SETTINGS",
    "update_status_bar_settings",
    "MEMORY_SETTINGS",
    "update_memory_settings",
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

# Status bar configuration
STATUS_BAR_SETTINGS = _build_status_bar_settings()

# Memory configuration
MEMORY_SETTINGS = _build_memory_settings()

# Tool approval modes
APPROVE_MODES = ("safe", "accept_edits", "danger")
CYCLEABLE_APPROVE_MODES = ("safe", "accept_edits")
APPROVE_MODE_LABELS = {
    "safe": "Safe",
    "accept_edits": "Accept Edits",
    "danger": "Danger",
}

def get_provider_config(provider: str):
    """Retrieve the configuration dictionary for a given provider.

    Args:
        provider (str): Provider name (e.g., 'local', 'openrouter', 'glm', 'openai').

    Returns:
        dict: Provider config from the PROVIDER_REGISTRY.
    """
    return PROVIDER_REGISTRY.get(provider, {})
