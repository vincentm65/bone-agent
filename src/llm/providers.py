"""Provider-specific request/response handlers.

This module isolates provider-specific API quirks into handler classes.
"""

import json
from typing import Optional, Dict, Any, Iterator
import requests

from exceptions import LLMResponseError


class OpenAIHandler:
    """Handler for OpenAI-compatible providers.

    Supports: OpenAI, OpenRouter, GLM, Gemini, Kimi, MiniMax
    """

    def build_headers(self, config: Dict[str, Any]) -> Dict[str, str]:
        """Build request headers."""
        headers = {"Content-Type": "application/json"}
        if config.get("type") == "api" and config.get("api_key"):
            headers["Authorization"] = f"Bearer {config['api_key']}"
        if "headers_extra" in config:
            headers.update(config["headers_extra"])
        return headers

    def build_payload(self, config: Dict[str, Any], messages: list,
                      tools: Optional[list] = None, stream: bool = True) -> Dict[str, Any]:
        """Build request payload."""
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


class ResponsesHandler:
    """Handler for OpenAI Responses API (/v1/responses).

    Used by Codex OAuth (ChatGPT subscription) authentication.
    The Responses API uses 'input' instead of 'messages' and returns
    'output' instead of 'choices', but accepts the same message format.
    """

    def build_headers(self, config: Dict[str, Any]) -> Dict[str, str]:
        """Build request headers."""
        headers = {"Content-Type": "application/json"}
        if config.get("type") == "api" and config.get("api_key"):
            headers["Authorization"] = f"Bearer {config['api_key']}"
        if "headers_extra" in config:
            headers.update(config["headers_extra"])
        return headers

    def build_payload(self, config: Dict[str, Any], messages: list,
                      tools: Optional[list] = None, stream: bool = True) -> Dict[str, Any]:
        """Build request payload for Codex backend Responses API.

        The ChatGPT backend requires:
        - 'instructions' for system prompt (extracted from messages)
        - 'store: false'
        - input_text content type for user messages
        """
        # Extract system messages into 'instructions' field
        system_parts = [m["content"] for m in messages if m.get("role") == "system"]
        instructions = "\n".join(system_parts) if system_parts else "You are a helpful assistant."

        # Convert non-system messages to Codex backend input format.
        # We preserve tool-call turns using Responses-native function_call and
        # function_call_output items so multi-step agent loops remain valid.
        codex_input = []
        for m in messages:
            if m.get("role") == "system":
                continue
            role = m.get("role", "user")
            content = m.get("content", "")

            if role == "assistant" and m.get("_responses_output"):
                codex_input.extend(m.get("_responses_output") or [])
                continue

            if role == "assistant" and m.get("tool_calls"):
                if content:
                    codex_input.append({
                        "role": "assistant",
                        "content": [{"type": "input_text", "text": content}]
                    })
                for tool_call in m.get("tool_calls", []):
                    function = tool_call.get("function", {})
                    codex_input.append({
                        "type": "function_call",
                        "call_id": tool_call.get("id"),
                        "name": function.get("name", ""),
                        "arguments": function.get("arguments", "{}"),
                    })
                continue

            if role == "tool":
                codex_input.append({
                    "type": "function_call_output",
                    "call_id": m.get("tool_call_id"),
                    "output": content,
                })
                continue

            content_type = "output_text" if role == "assistant" else "input_text"
            codex_input.append({
                "role": role,
                "content": [{"type": content_type, "text": content}]
            })

        payload = {
            **config.get("payload", {}),
            "instructions": instructions,
            "input": codex_input,
            "store": False,
            "stream": True,  # Codex backend requires stream=true
        }

        if "model" not in payload:
            model_name = config.get("api_model") or config.get("model")
            if model_name:
                payload["model"] = model_name

        if tools:
            payload["tools"] = [self._convert_tool_to_responses(tool) for tool in tools]

        if "temperature" not in payload and config.get("allow_temperature", True):
            payload["temperature"] = config.get("default_temperature", 0.1)
        if "top_p" not in payload and config.get("allow_top_p", True):
            payload["top_p"] = config.get("default_top_p", 0.9)

        return payload

    def parse_response(self, response_json: Dict[str, Any]) -> Dict[str, Any]:
        """Parse Responses API output into Chat Completions format."""
        return self._normalize_response(response_json)

    def parse_sse_response(self, response_text: str) -> Dict[str, Any]:
        """Parse a full SSE response body into Chat Completions format."""
        completed_response = None
        output_items = []

        for raw_line in response_text.splitlines():
            line = raw_line.strip()
            if not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str == "[DONE]":
                break
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError as e:
                raise LLMResponseError(
                    "Failed to decode SSE response from Codex backend",
                    details={"original_error": str(e)}
                )

            if data.get("type") == "response.output_item.done":
                item = data.get("item")
                if item:
                    output_items.append(item)
                continue

            if data.get("type") == "response.completed":
                completed_response = data.get("response")
                break

        if completed_response is None:
            raise LLMResponseError(
                "Codex backend returned streaming data without a completed response event"
            )

        if not completed_response.get("output") and output_items:
            completed_response = dict(completed_response)
            completed_response["output"] = output_items

        return self._normalize_response(completed_response)

    def parse_stream(self, response: requests.Response) -> Iterator[Dict[str, Any]]:
        """Parse streaming Responses API.

        The Responses API streams events like:
        - response.output_text.delta  → text chunk
        - response.completed          → final with usage
        """
        usage_data = None

        for line in response.iter_lines():
            if line:
                line = line.decode('utf-8')

                if line.startswith('data: '):
                    data_str = line[6:]
                    if data_str.strip() == '[DONE]':
                        break

                    try:
                        data = json.loads(data_str)

                        if 'error' in data:
                            error_msg = data.get('error', {}).get('message', 'Unknown streaming error')
                            raise LLMResponseError(
                                f"Streaming error: {error_msg}",
                                details={"error_data": data.get('error')}
                            )

                        event_type = data.get("type", "")

                        # Capture usage from completed event
                        if event_type == "response.completed":
                            resp = data.get("response", {})
                            if "usage" in resp:
                                usage_data = dict(resp["usage"])

                        # Text delta
                        if event_type == "response.output_text.delta":
                            delta = data.get("delta", "")
                            if delta:
                                yield delta

                    except json.JSONDecodeError as e:
                        raise LLMResponseError(
                            f"Failed to decode streaming response",
                            details={"original_error": str(e)}
                        )

        if usage_data:
            yield {'__usage__': usage_data}

    def _convert_tool_to_responses(self, tool: Dict[str, Any]) -> Dict[str, Any]:
        """Convert Chat Completions tool schema to Responses/Codex schema."""
        if tool.get("type") == "function" and "function" in tool:
            function = tool["function"]
            return {
                "type": "function",
                "name": function.get("name", ""),
                "description": function.get("description", ""),
                "parameters": self._normalize_json_schema(function.get("parameters", {})),
                "strict": False,
            }
        return tool

    def _normalize_response(self, response_json: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize Responses output into Chat Completions message shape."""
        output_items = response_json.get("output", [])
        content_parts = []
        tool_calls = []

        for item in output_items:
            item_type = item.get("type")

            if item_type == "function_call":
                call_id = item.get("call_id") or item.get("id")
                tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "{}"),
                    }
                })
                continue

            if item_type != "message":
                continue

            for c in item.get("content", []):
                if c.get("type") in {"output_text", "text"}:
                    text = c.get("text")
                    if text is not None:
                        content_parts.append(text)

        message = {"role": "assistant"}
        text_content = "\n".join(content_parts) if content_parts else ""
        if tool_calls:
            message["tool_calls"] = tool_calls
            message["content"] = text_content or None
        else:
            message["content"] = text_content

        # Strip server-side IDs — they're invalid on replay with store:false
        for item in output_items:
            item.pop("id", None)
        message["_responses_output"] = output_items

        return {
            "choices": [{
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }],
            "usage": response_json.get("usage", {}),
        }

    def _normalize_json_schema(self, schema: Any) -> Any:
        """Normalize JSON Schema for strict Responses function tools."""
        if not isinstance(schema, dict):
            return schema

        normalized = dict(schema)
        schema_type = normalized.get("type")

        if schema_type == "object":
            properties = normalized.get("properties", {})
            normalized["properties"] = {
                key: self._normalize_json_schema(value)
                for key, value in properties.items()
            }
            normalized.setdefault("additionalProperties", False)

        if schema_type == "array" and "items" in normalized:
            normalized["items"] = self._normalize_json_schema(normalized["items"])

        for key in ("anyOf", "oneOf", "allOf"):
            if key in normalized and isinstance(normalized[key], list):
                normalized[key] = [self._normalize_json_schema(item) for item in normalized[key]]

        return normalized


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
                      tools: Optional[list] = None, stream: bool = True) -> Dict[str, Any]:
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
                    # Already an array (Anthropic format), use as-is
                    anthropic_messages.append(msg)
                    continue

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
    "anthropic": AnthropicHandler,
    "local": OpenAIHandler,
    "codex_plan": ResponsesHandler,
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


__all__ = ['OpenAIHandler', 'AnthropicHandler', 'ResponsesHandler', 'get_handler']
