"""Streaming response assembler for agentic mode.

Consumes a StreamWrapper yielding mixed delta dicts and assembles them into
a complete message dict (content + tool_calls), matching the format that
non-streaming responses already produce.

Usage:
    stream = client.chat_completion(messages, stream=True, tools=tools)
    assembler = StreamingResponse(stream, console, debug_mode=False)
    message = assembler.consume()  # iterates stream, prints text, assembles tool_calls
    tool_calls = message.get("tool_calls")
    usage = assembler.usage
"""

import json
import sys
from typing import Any, Dict, List, Optional

from rich.text import Text


class StreamingResponse:
    """Assemble streaming deltas into a complete message dict.

    Text deltas are printed to stderr immediately (raw, no formatting).
    Tool call deltas are buffered and reassembled across chunks.
    """

    def __init__(self, stream, console=None, debug_mode: bool = False,
                 on_text=None, live=None):
        """
        Args:
            stream: StreamWrapper (or any iterable yielding deltas / __usage__ dicts).
            console: Rich Console instance (used for debug logging only).
            debug_mode: If True, log assembly details.
            on_text: Optional callback(str) invoked for each text token.
                     Defaults to printing to stderr.
            live: Optional Rich Live context. When set, streaming text is
                  rendered through Live (raw during streaming, swappable to
                  Markdown on completion) instead of raw stderr.
        """
        self._stream = stream
        self._console = console
        self._debug = debug_mode
        self._on_text = on_text
        self._live = live

        # Accumulated state
        self._text_parts: List[str] = []
        self._reasoning_parts: List[str] = []  # reasoning/thinking tokens (DeepSeek, etc.)
        self._tool_calls: Dict[int, Dict[str, Any]] = {}  # index -> partial tool call
        self._usage: Optional[Dict[str, Any]] = None

    def consume(self) -> Dict[str, Any]:
        """Iterate the stream, print text tokens, assemble tool calls.

        Returns:
            A message dict with 'role', 'content', and optionally 'tool_calls'
            — same shape as a non-streaming response["choices"][0]["message"].
        """
        for item in self._stream:
            if isinstance(item, dict) and '__usage__' in item:
                self._usage = item['__usage__']
                continue

            # OpenAI-style delta: {"content": "...", "tool_calls": [...]}
            if isinstance(item, dict):
                self._process_delta(item)
            elif isinstance(item, str):
                # Fallback: plain text string (legacy parse_stream behavior)
                self._print(item)
                self._text_parts.append(item)

        return self._build_message()

    @property
    def usage(self) -> Optional[Dict[str, Any]]:
        """Usage data captured from the stream's final chunk."""
        return self._usage

    def _process_delta(self, delta: Dict[str, Any]):
        """Process a single streaming delta dict.

        Expected shapes (OpenAI format):
            {"content": "some text"}
            {"tool_calls": [{"index": 0, "id": "call_xxx", "function": {"name": "f"}}]}
            {"tool_calls": [{"index": 0, "function": {"arguments": "{..."}}]}
            {"content": "text", "tool_calls": [...]}
        """
        # Handle text content
        content = delta.get("content")
        if content:
            self._print(content)
            self._text_parts.append(content)

        # Handle reasoning/thinking content (DeepSeek, etc.)
        reasoning = delta.get("reasoning_content")
        if reasoning is not None:
            self._reasoning_parts.append(reasoning)

        # Handle tool call fragments
        tool_calls = delta.get("tool_calls")
        if tool_calls:
            for tc_delta in tool_calls:
                idx = tc_delta.get("index", 0)
                if idx not in self._tool_calls:
                    self._tool_calls[idx] = {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }

                entry = self._tool_calls[idx]

                # Tool call id (sent once at the start)
                if tc_delta.get("id"):
                    entry["id"] = tc_delta["id"]

                # Function name (sent once at the start)
                func = tc_delta.get("function", {})
                if func.get("name"):
                    entry["function"]["name"] = func["name"]

                # Arguments (sent incrementally, concatenated)
                if func.get("arguments"):
                    entry["function"]["arguments"] += func["arguments"]

    def _build_message(self) -> Dict[str, Any]:
        """Build the final message dict from assembled parts."""
        message: Dict[str, Any] = {"role": "assistant"}

        # Include reasoning_content if present (required by DeepSeek thinking models)
        reasoning = "".join(self._reasoning_parts).strip()
        if reasoning:
            message["reasoning_content"] = reasoning

        # Collect assembled tool calls in index order
        assembled_tool_calls = []
        if self._tool_calls:
            for idx in sorted(self._tool_calls.keys()):
                assembled_tool_calls.append(self._tool_calls[idx])

        if assembled_tool_calls:
            message["tool_calls"] = assembled_tool_calls
            # Content may be None or a string alongside tool calls
            text = "".join(self._text_parts).strip()
            message["content"] = text if text else None
        else:
            message["content"] = "".join(self._text_parts)

        return message

    def _print(self, text: str):
        """Output text token via the configured callback (default: stderr).

        When a Rich Live context is provided, text is rendered through Live
        for atomic screen updates (raw text during streaming, swappable to
        Markdown on completion).
        """
        if self._live is not None:
            # Render through Rich Live — update with accumulated text so far
            self._live.update(Text("".join(self._text_parts) + text))
        elif self._on_text is None:
            # Default: print to stderr
            sys.stderr.write(text)
            sys.stderr.flush()
        elif callable(self._on_text):
            self._on_text(text)
        # If on_text is False, silently drop output (subagent mode)

    def close(self):
        """Close the underlying stream."""
        if hasattr(self._stream, 'close'):
            self._stream.close()
