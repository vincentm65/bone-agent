"""Provider/model capability checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from exceptions import ConfigurationError
from utils.multimodal import has_image_content


_IMAGE_MODEL_HINTS = (
    "4o",
    "vision",
    "vl",
    "gpt-5",
    "gpt-4.1",
    "gpt-4.5",
    "claude-3",
    "claude-4",
    "gemini",
    "qwen-vl",
    "glm-4v",
    "glm-4.1v",
    "kimi-vl",
    "minimax-01",
)

_TEXT_ONLY_MODEL_HINTS = (
    "embedding",
    "moderation",
    "rerank",
    "whisper",
)


@dataclass(frozen=True)
class CapabilityCheck:
    """Result of checking one request against provider capabilities."""

    ok: bool
    message: str = ""


def _normalise_model_name(registry: dict[str, Any]) -> str:
    return str(registry.get("api_model") or registry.get("model") or "").lower()


def supports_images(provider: str, registry: dict[str, Any]) -> bool:
    """Return whether this provider/model should accept image input."""
    override = registry.get("supports_images")
    if isinstance(override, bool):
        return override

    return True


def check_message_capabilities(provider: str, registry: dict[str, Any], messages: list[dict[str, Any]]) -> CapabilityCheck:
    """Validate message content before sending it to a provider."""
    if not has_image_content(messages):
        return CapabilityCheck(ok=True)

    if supports_images(provider, registry):
        return CapabilityCheck(ok=True)

    model = registry.get("model") or registry.get("api_model") or "unknown"
    return CapabilityCheck(
        ok=False,
        message=(
            f"The current provider/model does not advertise image input support "
            f"({provider}/{model}). Switch to a vision-capable model or set "
            f"supports_images: true for this provider config."
        ),
    )


def ensure_message_capabilities(provider: str, registry: dict[str, Any], messages: list[dict[str, Any]]) -> None:
    """Raise a clear configuration error when request content is unsupported."""
    result = check_message_capabilities(provider, registry, messages)
    if not result.ok:
        raise ConfigurationError(result.message)
