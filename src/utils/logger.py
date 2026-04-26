"""Markdown conversation logging module for saving chat history to readable markdown files."""

import json
import logging
from datetime import datetime
from utils.multimodal import content_text_for_logs
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


class MarkdownConversationLogger:
    """Logs conversations to Markdown format with tool call details."""

    MAX_CONTENT_LENGTH = 2000

    def __init__(self, conversations_dir: str = "conversations"):
        """Initialize markdown conversation logger.

        Args:
            conversations_dir: Directory to save conversation logs
        """
        self.conversations_dir = Path(conversations_dir)
        self.conversations_dir.mkdir(exist_ok=True)
        self.current_file: Optional[Path] = None

    def start_session(self):
        """Start a new conversation session."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.current_file = self.conversations_dir / f"conversation_{timestamp}.md"
        logger.info(f"Started markdown conversation logging to {self.current_file}")

        # Write header
        with open(self.current_file, 'w', encoding='utf-8') as f:
            f.write(f"# Conversation Log\n\n")
            f.write(f"**Started:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write("---\n\n")

    def _format_tool_call(self, tool_call: Dict[str, Any]) -> str:
        """Format a tool call for markdown display.

        Args:
            tool_call: Tool call dict with id, type, function

        Returns:
            Formatted markdown string
        """
        fn = tool_call.get("function", {})
        name = fn.get("name", "unknown")
        arguments = fn.get("arguments", "{}")
        args_str = self._format_json_value(arguments)

        return f"""### {name}

```json
{args_str}
```"""

    def _format_json_value(self, value: Any) -> str:
        """Format a value as JSON for markdown display.

        Args:
            value: Value to format (string, dict, or other)

        Returns:
            Formatted JSON string, or string representation if not JSON-serializable
        """
        try:
            if isinstance(value, str):
                parsed = json.loads(value)
            else:
                parsed = value
            return json.dumps(parsed, indent=2, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            return str(value)

    def _format_tool_call_inline(self, arguments: Any) -> str:
        """Format tool call arguments as JSON for inline display.

        Args:
            arguments: Tool call arguments (string or dict)

        Returns:
            Formatted JSON string
        """
        args_str = self._format_json_value(arguments)
        return f"```json\n{args_str}\n```"

    def _format_tool_result(self, message: Dict[str, Any]) -> str:
        """Format a tool result for markdown display.

        Args:
            message: Tool result message with role, tool_call_id, content

        Returns:
            Formatted markdown string
        """
        content = content_text_for_logs(message.get("content", ""))

        # Truncate very long outputs
        if len(content) > self.MAX_CONTENT_LENGTH:
            content = content[:self.MAX_CONTENT_LENGTH] + "\n\n... (truncated)"

        # Try to format as code if it looks like structured output
        if content and (content.startswith("{") or content.startswith("[")):
            try:
                parsed = json.loads(content)
                content = json.dumps(parsed, indent=2, ensure_ascii=False)
                return f"```\n{content}\n```\n"
            except json.JSONDecodeError:
                pass

        return f"```\n{content}\n```\n"

    def _format_message(self, message: Dict[str, Any], skip_tool_calls: bool = False) -> str:
        """Convert a message dict to markdown format.

        Args:
            message: Message dict with role, content, tool_calls, etc.
            skip_tool_calls: If True, don't include tool_calls section

        Returns:
            Formatted markdown string
        """
        role = message.get("role", "unknown")
        content = content_text_for_logs(message.get("content", ""))

        if role == "user":
            emoji = "👤"
            title = "User"
        elif role == "assistant":
            emoji = "🤖"
            title = "Assistant"
        elif role == "tool":
            # Tool results are handled separately with their tool calls
            return None
        elif role == "system":
            emoji = "⚙️"
            title = "System"
        else:
            emoji = "📝"
            title = role.capitalize()

        md = f"\n## {emoji} {title}\n\n"

        # Add content if present
        if content:
            md += f"{content}\n\n"

        # Add tool calls if present (skip when skip_tool_calls=True)
        if not skip_tool_calls and message.get("tool_calls"):
            md += "### 🔧 Tool Calls\n\n"
            for tc in message["tool_calls"]:
                md += self._format_tool_call(tc) + "\n"

        return md

    def log_message(self, message: Dict[str, Any]):
        """Append a message to the current markdown file.

        Args:
            message: Message dict with 'role' and 'content' keys, optionally 'tool_calls'
        """
        if not self.current_file:
            self.start_session()

        # Check if this is a tool result - we'll handle it differently
        if message.get("role") == "tool":
            # Find the associated tool call by tool_call_id
            tool_call_id = message.get("tool_call_id")
            formatted = f"\n### 📋 Tool Result (ID: `{tool_call_id}`)\n\n"
            formatted += self._format_tool_result(message) + "\n"
        else:
            formatted = self._format_message(message)

        if formatted:
            with open(self.current_file, 'a', encoding='utf-8') as f:
                f.write(formatted)

    def rewrite_log(self, messages: List[Dict[str, Any]]):
        """Rewrite the current markdown log to match the provided messages.

        Args:
            messages: Full message list to persist
        """
        if not self.current_file:
            self.start_session()

        # Rewrite entire file
        with open(self.current_file, 'w', encoding='utf-8') as f:
            # Write header
            f.write(f"# Conversation Log\n\n")
            f.write(f"**Started:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write(f"**Last Updated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write("---\n\n")

            # Track tool calls to pair with results
            pending_tool_calls = {}

            for message in messages:
                role = message.get("role")

                if role == "assistant" and message.get("tool_calls"):
                    # Store tool calls for later pairing with results
                    for tc in message["tool_calls"]:
                        pending_tool_calls[tc["id"]] = tc

                    # Write the assistant message (skip tool_calls section)
                    formatted = self._format_message(message, skip_tool_calls=True)
                    if formatted:
                        f.write(formatted)

                elif role == "tool":
                    # This is a tool result, pair it with the call
                    tool_call_id = message.get("tool_call_id")
                    tc = pending_tool_calls.get(tool_call_id)

                    if tc:
                        # Write tool call with result together
                        fn = tc.get("function", {})
                        name = fn.get("name", "unknown")
                        arguments = fn.get("arguments", "{}")

                        f.write(f"\n### 🔧 Tool Call: {name}\n\n")
                        f.write(self._format_tool_call_inline(arguments) + "\n\n")
                        f.write(f"**Result:**\n\n")
                    else:
                        # Orphaned result (no matching call)
                        f.write(f"\n### 🔧 Tool Result (ID: `{tool_call_id}`)\n\n")

                    f.write(self._format_tool_result(message) + "\n")

                else:
                    # Regular message (user, system)
                    formatted = self._format_message(message)
                    if formatted:
                        f.write(formatted)

            # Write footer
            f.write("\n---\n\n")
            f.write(f"*End of conversation*\n")

    def end_session(self):
        """End the current conversation logging session."""
        if not self.current_file:
            return

        # Add footer
        with open(self.current_file, 'a', encoding='utf-8') as f:
            f.write("\n---\n\n")
            f.write(f"**Ended:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

        logger.info(f"Ended markdown conversation session: {self.current_file.name}")
        self.current_file = None
