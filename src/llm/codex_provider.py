"""Codex provider adapter.

Codex is intentionally isolated from the normal provider handlers because it is
not a Chat Completions-compatible API. It targets the ChatGPT Codex Responses
backend and adapts that protocol back into vmCode's OpenAI-style internal shape.
"""

import copy
import hashlib
import json
from typing import Any, Dict, Iterator, Optional

import requests

from exceptions import LLMResponseError
from utils.multimodal import openai_blocks_to_codex


class CodexResponsesHandler:
    """Adapter for the ChatGPT Codex Responses backend.

    Codex-specific behavior kept here:
    - Uses `instructions` + `input` instead of Chat Completions `messages`.
    - Always sends `stream: true`; the backend returns SSE even for logical
      non-streaming agent calls.
    - Stores `_responses_output` replay metadata so tool-call turns can be sent
      back in Responses-native form while using `store: false`.
    """

    supports_sse_response_fallback = True

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
        """Build request payload for Codex backend Responses API."""
        system_parts = [m["content"] for m in messages if m.get("role") == "system"]
        instructions = "\n".join(system_parts) if system_parts else "You are a helpful assistant."

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
                        "content": openai_blocks_to_codex(content, assistant=True)
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

            codex_input.append({
                "role": role,
                "content": openai_blocks_to_codex(content, assistant=(role == "assistant"))
            })

        payload = {
            **config.get("payload", {}),
            "instructions": instructions,
            "input": codex_input,
            "store": False,
            "stream": True,
        }

        if "model" not in payload:
            model_name = config.get("api_model") or config.get("model")
            if model_name:
                payload["model"] = model_name

        if tools:
            payload["tools"] = [self._convert_tool_to_responses(tool) for tool in tools]

        if "prompt_cache_key" not in payload:
            model = payload.get("model") or "unknown-model"
            payload["prompt_cache_key"] = self._build_prompt_cache_key(
                model=model,
                instructions=instructions,
                tools=payload.get("tools"),
            )

        if "temperature" not in payload and config.get("allow_temperature", True):
            payload["temperature"] = config.get("default_temperature", 0.1)
        if "top_p" not in payload and config.get("allow_top_p", True):
            payload["top_p"] = config.get("default_top_p", 0.9)

        return payload

    def _build_prompt_cache_key(
        self,
        *,
        model: str,
        instructions: str,
        tools: Optional[list] = None,
    ) -> str:
        """Build a stable prompt-cache key for the reusable Codex prefix."""
        cache_scope = {
            "model": model,
            "instructions": instructions,
            "tools": tools or [],
        }
        canonical = json.dumps(
            cache_scope,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        cache_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
        return f"bone-agent:{cache_hash}"

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
        """Parse streaming Responses API."""
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

                        if event_type == "response.completed":
                            resp = data.get("response", {})
                            if "usage" in resp:
                                usage_data = self._normalize_usage(resp["usage"])

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
        raw_usage = response_json.get("usage", {})
        usage = self._normalize_usage(raw_usage)

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

        replay_items = copy.deepcopy(output_items)
        for item in replay_items:
            item.pop("id", None)
        message["_responses_output"] = replay_items

        return {
            "choices": [{
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }],
            "usage": usage,
        }

    def _normalize_usage(self, usage: Any) -> Dict[str, Any]:
        """Normalize Codex Responses usage into vmCode's OpenAI-style usage shape."""
        if not isinstance(usage, dict):
            return {}

        normalized = dict(usage)

        input_tokens = normalized.get("input_tokens")
        output_tokens = normalized.get("output_tokens")

        if normalized.get("prompt_tokens") is None and input_tokens is not None:
            normalized["prompt_tokens"] = input_tokens
        if normalized.get("completion_tokens") is None and output_tokens is not None:
            normalized["completion_tokens"] = output_tokens
        if normalized.get("total_tokens") is None:
            prompt_tokens = normalized.get("prompt_tokens")
            completion_tokens = normalized.get("completion_tokens")
            if prompt_tokens is not None and completion_tokens is not None:
                normalized["total_tokens"] = prompt_tokens + completion_tokens

        input_details = normalized.get("input_tokens_details")
        if isinstance(input_details, dict) and input_details.get("cached_tokens") is not None:
            cached_tokens = input_details["cached_tokens"]
            if normalized.get("prompt_tokens_details") is None:
                normalized["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
            elif isinstance(normalized["prompt_tokens_details"], dict):
                normalized["prompt_tokens_details"].setdefault("cached_tokens", cached_tokens)
            normalized.setdefault("cached_tokens", cached_tokens)

        return normalized

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
