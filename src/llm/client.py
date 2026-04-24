"""LLM client for making API requests to various providers."""

import logging

import requests
from llm import config as config_module
from llm.config import PROVIDER_REGISTRY, get_provider_config, get_providers
from llm.providers import get_handler
from exceptions import LLMConnectionError, LLMResponseError, ConfigurationError
from utils.validation import validate_api_url

logger = logging.getLogger(__name__)

# Connection/read timeouts (seconds)
_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 120


class StreamWrapper:
    """Wraps streaming response generator with cleanup capability."""

    def __init__(self, response, generator):
        self._response = response
        self._generator = generator

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._generator)

    def close(self):
        """Close underlying HTTP connection."""
        if self._response:
            self._response.close()


class LLMClient:
    def __init__(self, provider=None):
        """Initialize LLM client.

        Args:
            provider: Provider name. If None, uses global LLM_PROVIDER from config.
        """
        self.provider = provider or config_module.LLM_PROVIDER
        self.handler = get_handler(self.provider)
        self.config = self._get_provider_config()

    @property
    def model(self) -> str:
        """Return configured model name, if any."""
        return str(self.config.get("payload", {}).get("model") or "")

    def _get_provider_config(self):
        """Build provider config from PROVIDER_REGISTRY."""
        registry = get_provider_config(self.provider)
        if not registry:
            raise ConfigurationError(f"Unknown provider: {self.provider}")

        # Build headers using handler
        headers = self.handler.build_headers(registry)

        # Build payload with model name
        payload = {}
        model_name = registry.get("api_model") or registry.get("model")
        if model_name:
            payload["model"] = model_name

        url = f"{registry['api_base']}{registry['endpoint']}"
        valid, err = validate_api_url(url)
        if not valid:
            raise ConfigurationError(
                f"Insecure API endpoint for provider '{self.provider}': {err}"
            )

        return {
            "url": url,
            "headers": headers,
            "payload": payload,
            "error_prefix": registry["error_prefix"],
            "registry": registry
        }

    def chat_completion(self, messages, stream=True, tools=None):
        """Make a chat completion request.

        Args:
            messages: List of message dicts
            stream: Whether to stream the response
            tools: Optional list of tool definitions

        Returns:
            StreamWrapper if stream=True, else response dict
        """
        config = self.config
        registry = config["registry"]

        # Build payload using handler
        payload = self.handler.build_payload(registry, messages, tools, stream)

        try:
            response = requests.post(
                config["url"],
                headers=config["headers"],
                json=payload,
                stream=stream,
                verify=True,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )

            # For better debugging, include response text on 4xx errors
            if not response.ok:
                error_details = response.text if response.text else str(response.status_code)
                raise LLMConnectionError(
                    f"Error communicating with {config['error_prefix']}",
                    details={
                        "provider": self.provider,
                        "original_error": error_details,
                        "status_code": response.status_code,
                    }
                )
            response.raise_for_status()

            if stream:
                return StreamWrapper(
                    response,
                    self.handler.parse_stream(response)
                )
            else:
                try:
                    response_json = response.json()
                    return self.handler.parse_response(response_json)
                except ValueError:
                    if getattr(self.handler, "supports_sse_response_fallback", False):
                        return self.handler.parse_sse_response(response.text)
                    raise

        except requests.exceptions.RequestException as e:
            raise LLMConnectionError(
                f"Error communicating with {config['error_prefix']}",
                details={"provider": self.provider, "original_error": str(e)}
            )

    def switch_provider(self, new_provider):
        """Switch to a different provider.

        Args:
            new_provider: Name of the provider to switch to.

        Returns:
            True if successful, False if provider not found.
        """
        if new_provider in get_providers():
            self.provider = new_provider
            self.handler = get_handler(new_provider)
            self.config = self._get_provider_config()
            return True
        return False

    def sync_provider_from_config(self):
        """Sync this client's provider and config with the current config.

        This should be called after config is reloaded from disk.
        """
        current_provider = config_module.LLM_PROVIDER
        if self.provider != current_provider:
            self.provider = current_provider
            self.handler = get_handler(current_provider)
            self.config = self._get_provider_config()
            return True
        # Even if provider hasn't changed, config values (model, api_key) might have
        self.config = self._get_provider_config()
        return False


__all__ = ['LLMClient']
