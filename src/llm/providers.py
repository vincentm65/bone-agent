"""Provider-specific request/response handlers.

This module isolates provider-specific API quirks into handler classes.
"""

import json
from typing import Optional, Dict, Any, Iterator
import requests

from exceptions import LLMResponseError
from utils.multimodal import openai_blocks_to_anthropic
from .codex_provider import CodexResponsesHandler


class OpenAIHandler:
    """Handler for OpenAI-compatible providers.

    Supports: OpenAI, OpenRouter, GLM, Gemini, Kimi, MiniMax
    """

    def _normalize_tool_messages(self, messages: list) -> list:
        """Return an OpenAI-compatible message sequence for tool calls.

        OpenAI-compatible APIs require tool result messages to immediately
        follow the assistant message that declared their ``tool_calls``. Some
        local history operations can leave stale/orphaned tool messages behind;
        DeepSeek rejects those requests with a 400, so remove invalid fragments
        at the provider boundary.
        """
        normalized = []
        i = 0

        while i < len(messages):
            msg = messages[i]

            if msg.get("role") == "tool":
                # Orphaned tool result: there is no immediately preceding
                # assistant(tool_calls) block in the request.
                i += 1
                continue

            tool_calls = msg.get("tool_calls") if msg.get("role") == "assistant" else None
            if not tool_calls:
                normalized.append(msg)
                i += 1
                continue

            expected_ids = {
                tc.get("id")
                for tc in tool_calls
                if tc.get("id")
            }
            tool_results = []
            seen_ids = set()
            j = i + 1

            while j < len(messages) and messages[j].get("role") == "tool":
                tool_msg = messages[j]
                tool_call_id = tool_msg.get("tool_call_id")
                if tool_call_id in expected_ids and tool_call_id not in seen_ids:
                    tool_results.append(tool_msg)
                    seen_ids.add(tool_call_id)
                j += 1

            if expected_ids and seen_ids == expected_ids:
                normalized.append(msg)
                normalized.extend(tool_results)
            else:
                # Incomplete tool block. Keep any assistant text, but do not
                # send dangling tool_calls or their tool results.
                assistant_msg = dict(msg)
                assistant_msg.pop("tool_calls", None)
                content = assistant_msg.get("content")
                if isinstance(content, str):
                    if content.strip():
                        normalized.append(assistant_msg)
                elif content:
                    normalized.append(assistant_msg)

            i = j

        return normalized

    def build_headers(self, config: Dict[str, Any]) -> Dict[str, str]:
        """Build request headers."""
        headers = {"Content-Type": "application/json"}
        if config.get("type") == "api" and config.get("api_key"):
            headers["Authorization"] = f"Bearer {config['api_key']}"
        if "headers_extra" in config:
            headers.update(config["headers_extra"])
        return headers

    def build_payload(self, config: Dict[str, Any], messages: list,
                      tools: Optional[list] = None, stream: bool = True,
                      conversation_id: Optional[str] = None) -> Dict[str, Any]:
        """Build request payload."""
        messages = self._normalize_tool_messages(messages)
        payload = {**config.get("payload", {}), "messages": messages, "stream": stream}

        # Ensure model is set from config if not in payload
        if "model" not in payload:
            model_name = config.get("api_model") or config.get("model")
            if model_name:
                payload["model"] = model_name

        # Add tools if provided (OpenAI format)
        if tools:
            payload["tools"] = tools

        # Set default parameters if not in config
        if "temperature" not in payload and config.get("allow_temperature", True):
            payload["temperature"] = config.get("default_temperature", 0.1)
        if "top_p" not in payload and config.get("allow_top_p", True):
            payload["top_p"] = config.get("default_top_p", 0.9)

        if config.get("provider") == "bone" and payload.get("model", "").startswith("deepseek/"):
            payload["provider"] = {"order": ["deepseek"]}

        return payload

    def parse_response(self, response_json: Dict[str, Any]) -> Dict[str, Any]:
        """Parse non-streaming response (already in OpenAI format)."""
        return response_json

    def parse_stream(self, response: requests.Response) -> Iterator[Dict[str, Any]]:
        """Parse streaming response.

        Yields text chunks, and finally yields a dict with __usage__ key.
        """
        usage_data = None

        for line in response.iter_lines():
            if line:
                line = line.decode('utf-8')

                # Skip OpenRouter comments (start with ':')
                if line.startswith(':'):
                    continue

                if line.startswith('data: '):
                    data_str = line[6:]
                    if data_str.strip() == '[DONE]':
                        break

                    try:
                        data = json.loads(data_str)

                        # Check for mid-stream errors
                        if 'error' in data:
                            error_msg = data.get('error', {}).get('message', 'Unknown streaming error')
                            raise LLMResponseError(
                                f"Streaming error: {error_msg}",
                                details={"error_data": data.get('error')}
                            )

                        # Capture usage data if present (usually in final chunk)
                        if 'usage' in data:
                            usage_data = dict(data['usage'])
                            # Promote top-level cost into usage dict (OpenRouter places it here)
                            if 'cost' in data:
                                usage_data['cost'] = data['cost']

                        choices = data.get('choices', [])
                        if choices:
                            delta = choices[0].get('delta', {})
                            content = delta.get('content')
                            if content is not None:
                                yield content

                    except json.JSONDecodeError as e:
                        raise LLMResponseError(
                            f"Failed to decode streaming response",
                            details={"original_error": str(e)}
                        )

        # Yield usage data as final item if captured
        if usage_data:
            yield {'__usage__': usage_data}


class AnthropicHandler:
    """Handler for Anthropic API.

    Anthropic has significant differences from OpenAI:
    - Different endpoint (/messages vs /chat/completions)
    - Different message format (content arrays vs strings)
    - Different tool format (flat vs nested)
    - Different streaming (SSE with event types vs data: lines)
    - Different headers (x-api-key vs Authorization: Bearer)
    - Different parameters (requires max_tokens, forbids top_p with temperature)
    """

    def build_headers(self, config: Dict[str, Any]) -> Dict[str, str]:
        """Build request headers (Anthropic uses x-api-key)."""
        headers = {"Content-Type": "application/json"}
        if config.get("type") == "api" and config.get("api_key"):
            headers["x-api-key"] = config['api_key']
        if "headers_extra" in config:
            headers.update(config["headers_extra"])
        return headers

    def build_payload(self, config: Dict[str, Any], messages: list,
                      tools: Optional[list] = None, stream: bool = True,
                      conversation_id: Optional[str] = None) -> Dict[str, Any]:
        """Build request payload (Anthropic format)."""
        # Extract system messages to top-level parameter
        system_messages = [msg["content"] for msg in messages if msg.get("role") == "system"]
        system_content = "\n".join(system_messages) if system_messages else None
        non_system_messages = [msg for msg in messages if msg.get("role") != "system"]

        # Convert messages and tools to Anthropic format
        anthropic_messages = self._convert_messages_to_anthropic(non_system_messages)
        anthropic_tools = self._convert_tools_to_anthropic(tools) if tools else None

        payload = {**config.get("payload", {}), "messages": anthropic_messages, "stream": stream}

        # Ensure model is set from config if not in payload
        if "model" not in payload:
            model_name = config.get("api_model") or config.get("model")
            if model_name:
                payload["model"] = model_name

        if system_content:
            payload["system"] = system_content
        if anthropic_tools:
            payload["tools"] = anthropic_tools

        # Set default parameters (Anthropic requires max_tokens)
        if "temperature" not in payload and config.get("allow_temperature", True):
            payload["temperature"] = config.get("default_temperature", 0.1)
        if "max_tokens" not in payload:
            payload["max_tokens"] = config.get("max_tokens", 4096)
        
        # Anthropic doesn't allow both temperature and top_p
        # Only set top_p if temperature is not set
        if "temperature" not in payload and "top_p" not in payload:
            payload["top_p"] = config.get("default_top_p", 0.9)
        
        return payload

    def parse_response(self, response_json: Dict[str, Any]) -> Dict[str, Any]:
        """Convert Anthropic response format to OpenAI-style format."""
        # Anthropic format: {"content": [{"type": "text", "text": "..."}], "usage": {...}}
        # OpenAI format: {"choices": [{"message": {"content": "..."}}], "usage": {...}}

        # Convert Anthropic usage format (input_tokens/output_tokens) to OpenAI format (prompt_tokens/completion_tokens)
        # Anthropic's input_tokens does NOT include cache tokens; total input =
        #   input_tokens + cache_read_input_tokens + cache_creation_input_tokens
        anthropic_usage = response_json.get("usage", {})
        cache_read = anthropic_usage.get('cache_read_input_tokens', 0)
        cache_creation = anthropic_usage.get('cache_creation_input_tokens', 0)
        prompt_tokens = anthropic_usage.get('input_tokens', 0) + cache_read + cache_creation
        completion_tokens = anthropic_usage.get('output_tokens', 0)
        openai_format_usage = {
            'prompt_tokens': prompt_tokens,
            'completion_tokens': completion_tokens,
            'total_tokens': prompt_tokens + completion_tokens,
        }
        # Preserve Anthropic cache token fields for the token tracker
        if 'cache_read_input_tokens' in anthropic_usage:
            openai_format_usage['cache_read_input_tokens'] = anthropic_usage['cache_read_input_tokens']
        if 'cache_creation_input_tokens' in anthropic_usage:
            openai_format_usage['cache_creation_input_tokens'] = anthropic_usage['cache_creation_input_tokens']
        # Preserve non-cache input count so cost estimation can bill only the
        # non-cache portion without relying on fragile prompt_tokens subtraction.
        if 'input_tokens' in anthropic_usage:
            openai_format_usage['input_tokens'] = anthropic_usage['input_tokens']

        result = {
            "choices": [],
            "usage": openai_format_usage
        }

        # Extract content from Anthropic's content array
        content_blocks = response_json.get("content", [])
        text_parts = []
        tool_calls = []

        for block in content_blocks:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                # Convert Anthropic tool_use to OpenAI tool_calls format
                tool_calls.append({
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input", {}))
                    }
                })

        # Build OpenAI-style message
        message = {"role": "assistant"}

        # Include either text content or tool calls
        if tool_calls:
            message["content"] = None
            message["tool_calls"] = tool_calls
        else:
            message["content"] = "".join(text_parts)

        result["choices"].append({"message": message})

        return result

    def parse_stream(self, response: requests.Response) -> Iterator[Dict[str, Any]]:
        """Parse Anthropic's SSE-based streaming response.

        Yields text chunks, and finally yields a dict with __usage__ key.

        Anthropic splits usage across two events:
        - message_start: contains input_tokens
        - message_delta: contains output_tokens
        We merge both and convert to OpenAI format (prompt_tokens/completion_tokens).
        """
        usage_data = {}

        for line in response.iter_lines():
            if line:
                line = line.decode('utf-8')

                # Anthropic uses SSE format: "event: <type>" followed by "data: <json>"
                if line.startswith('data: '):
                    data_str = line[6:]
                    try:
                        data = json.loads(data_str)

                        # Check for errors
                        if data.get('type') == 'error':
                            error_msg = data.get('error', {}).get('message', 'Unknown error')
                            raise LLMResponseError(
                                f"Anthropic streaming error: {error_msg}",
                                details={"error_data": data.get('error')}
                            )

                        # Capture input_tokens from message_start events
                        if data.get('type') == 'message_start':
                            message_usage = data.get('message', {}).get('usage', {})
                            if message_usage:
                                usage_data.update(message_usage)

                        # Capture output_tokens from message_delta events
                        if data.get('type') == 'message_delta' and 'usage' in data:
                            usage_data.update(data['usage'])

                        # Extract text from content_block_delta events
                        if data.get('type') == 'content_block_delta':
                            delta = data.get('delta', {})
                            if delta.get('type') == 'text_delta':
                                text = delta.get('text', '')
                                if text:
                                    yield text

                    except json.JSONDecodeError as e:
                        raise LLMResponseError(
                            f"Failed to decode Anthropic streaming response",
                            details={"original_error": str(e)}
                        )

        # Yield usage data as final item if captured
        # Convert Anthropic format (input_tokens/output_tokens) to OpenAI format (prompt_tokens/completion_tokens)
        # Anthropic's input_tokens does NOT include cache tokens; total input =
        #   input_tokens + cache_read_input_tokens + cache_creation_input_tokens
        if usage_data:
            cache_read = usage_data.get('cache_read_input_tokens', 0)
            cache_creation = usage_data.get('cache_creation_input_tokens', 0)
            prompt_tokens = usage_data.get('input_tokens', 0) + cache_read + cache_creation
            completion_tokens = usage_data.get('output_tokens', 0)
            openai_format_usage = {
                'prompt_tokens': prompt_tokens,
                'completion_tokens': completion_tokens,
                'total_tokens': prompt_tokens + completion_tokens,
            }
            # Preserve Anthropic cache token fields for the token tracker
            if 'cache_read_input_tokens' in usage_data:
                openai_format_usage['cache_read_input_tokens'] = usage_data['cache_read_input_tokens']
            if 'cache_creation_input_tokens' in usage_data:
                openai_format_usage['cache_creation_input_tokens'] = usage_data['cache_creation_input_tokens']
            # Preserve non-cache input count for accurate cost estimation
            if 'input_tokens' in usage_data:
                openai_format_usage['input_tokens'] = usage_data['input_tokens']
            yield {'__usage__': openai_format_usage}

    @staticmethod
    def _convert_tools_to_anthropic(openai_tools: list) -> list:
        """Convert OpenAI-style tool definitions to Anthropic format.

        OpenAI format: {"type": "function", "function": {"name": "...", "parameters": {...}}}
        Anthropic format: {"name": "...", "description": "...", "input_schema": {...}}
        """
        anthropic_tools = []

        for openai_tool in openai_tools:
            if openai_tool.get("type") == "function":
                func = openai_tool.get("function", {})
                anthropic_tool = {
                    "name": func.get("name"),
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {"type": "object", "properties": {}})
                }
                anthropic_tools.append(anthropic_tool)

        return anthropic_tools

    @staticmethod
    def _convert_messages_to_anthropic(openai_messages: list) -> list:
        """Convert OpenAI-style messages to Anthropic format.

        Anthropic requires all content to be an array, not a string.

        OpenAI format:
            {"role": "user", "content": "text"}
            {"role": "tool", "content": "...", "tool_call_id": "..."}

        Anthropic format:
            {"role": "user", "content": [{"type": "text", "text": "..."}]}
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "...", "content": "..."}]}
        """
        anthropic_messages = []

        for msg in openai_messages:
            # Handle tool result messages
            if msg.get("role") == "tool":
                anthropic_msg = {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.get("tool_call_id"),
                            "content": msg.get("content", "")
                        }
                    ]
                }
                anthropic_messages.append(anthropic_msg)
            # Handle user and assistant messages - convert string content to array
            elif msg.get("role") in ("user", "assistant"):
                content = msg.get("content", "")
                tool_calls = msg.get("tool_calls")

                # Build content blocks array
                content_blocks = []

                # Add text content if present
                if isinstance(content, str) and content.strip():
                    content_blocks.append({
                        "type": "text",
                        "text": content
                    })
                elif isinstance(content, list):
                    content_blocks.extend(openai_blocks_to_anthropic(content))

                # Add tool_use blocks if present (for assistant messages with tool calls)
                if tool_calls:
                    for tool_call in tool_calls:
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tool_call.get("id"),
                            "name": tool_call.get("function", {}).get("name"),
                            "input": json.loads(tool_call.get("function", {}).get("arguments", "{}"))
                        })

                # Only add message if we have content blocks (text or tool_use)
                if content_blocks:
                    anthropic_msg = {
                        "role": msg.get("role"),
                        "content": content_blocks
                    }
                    anthropic_messages.append(anthropic_msg)
            else:
                # Other message types, pass through
                anthropic_messages.append(msg)

        return anthropic_messages


# Handler registry - maps provider names to handler classes
HANDLER_REGISTRY = {
    "openai": OpenAIHandler,
    "openrouter": OpenAIHandler,
    "glm": OpenAIHandler,
    "glm_plan": OpenAIHandler,
    "gemini": OpenAIHandler,
    "minimax": AnthropicHandler,
    "minimax_plan": AnthropicHandler,
    "kimi": OpenAIHandler,
    "deepseek": OpenAIHandler,
    "anthropic": AnthropicHandler,
    "local": OpenAIHandler,
    "codex": CodexResponsesHandler,
}


def get_handler(provider_name: str):
    """Get handler instance for the given provider.

    Args:
        provider_name: Name of the provider

    Returns:
        Handler instance for the provider
    """
    handler_class = HANDLER_REGISTRY.get(provider_name.lower(), OpenAIHandler)
    return handler_class()


__all__ = ['OpenAIHandler', 'AnthropicHandler', 'CodexResponsesHandler', 'get_handler']
