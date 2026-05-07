"""Helpers for provider-neutral multimodal message content."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any, Iterable

from utils.terminal_sanitize import strip_terminal_control


_PLACEHOLDER_RE = re.compile(r"\[Image #(\d+)\]")


@dataclass(frozen=True)
class ImageAttachment:
    """Prompt image attachment referenced by a stable placeholder."""

    index: int
    data: bytes
    mime_type: str

    @property
    def placeholder(self) -> str:
        return f"[Image #{self.index}]"


def image_data_url(data: bytes, mime_type: str) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def build_message_content(text: str, attachments: Iterable[ImageAttachment]) -> str | list[dict[str, Any]]:
    """Build text-only or OpenAI-style multimodal message content."""
    text = strip_terminal_control(text)
    attachment_map = {attachment.index: attachment for attachment in attachments}
    if not attachment_map:
        return text

    blocks: list[dict[str, Any]] = []
    cursor = 0
    used: set[int] = set()

    for match in _PLACEHOLDER_RE.finditer(text):
        preceding = text[cursor:match.start()]
        if preceding:
            blocks.append({"type": "text", "text": preceding})

        index = int(match.group(1))
        attachment = attachment_map.get(index)
        if attachment:
            blocks.append({
                "type": "image_url",
                "image_url": {"url": image_data_url(attachment.data, attachment.mime_type)},
            })
            used.add(index)
        else:
            blocks.append({"type": "text", "text": match.group(0)})
        cursor = match.end()

    trailing = text[cursor:]
    if trailing:
        blocks.append({"type": "text", "text": trailing})

    for index in sorted(set(attachment_map) - used):
        attachment = attachment_map[index]
        if blocks and blocks[-1].get("type") == "text":
            blocks[-1]["text"] = f"{blocks[-1]['text']}\n\n{attachment.placeholder}"
        else:
            blocks.append({"type": "text", "text": attachment.placeholder})
        blocks.append({
            "type": "image_url",
            "image_url": {"url": image_data_url(attachment.data, attachment.mime_type)},
        })

    return blocks or text


def content_text_for_logs(content: Any) -> str:
    """Return content text with image payloads redacted for logs/summaries."""
    if isinstance(content, str):
        return strip_terminal_control(content)
    if not isinstance(content, list):
        return str(content) if content is not None else ""

    parts: list[str] = []
    image_count = 0
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        block_type = block.get("type")
        if block_type == "text":
            parts.append(str(block.get("text", "")))
        elif block_type == "image_url":
            image_count += 1
            parts.append(f"[Image #{image_count}]")
        elif block_type == "image":
            image_count += 1
            parts.append(f"[Image #{image_count}]")
        else:
            parts.append(str(block))

    return strip_terminal_control("".join(parts))


def has_image_content(messages: Iterable[dict[str, Any]]) -> bool:
    """Return True if any message contains image blocks."""
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in {"image_url", "image"}:
                    return True
    return False


def openai_blocks_to_anthropic(content: Any) -> Any:
    """Convert OpenAI-style content blocks to Anthropic content blocks."""
    if not isinstance(content, list):
        return content

    converted: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            converted.append({"type": "text", "text": str(block)})
            continue

        if block.get("type") == "text":
            text = block.get("text", "")
            if text:
                converted.append({"type": "text", "text": text})
            continue

        if block.get("type") == "image_url":
            url = (block.get("image_url") or {}).get("url", "")
            header, _, payload = url.partition(",")
            if header.startswith("data:") and ";base64" in header and payload:
                media_type = header[5:].split(";", 1)[0]
                converted.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": payload,
                    },
                })
            else:
                converted.append({"type": "text", "text": "[Unsupported image URL omitted]"})
            continue

        converted.append(block)

    return converted


def openai_blocks_to_codex(content: Any, *, assistant: bool = False) -> list[dict[str, Any]]:
    """Convert internal content to Responses API content items."""
    text_type = "output_text" if assistant else "input_text"
    if isinstance(content, str):
        return [{"type": text_type, "text": content}]
    if not isinstance(content, list):
        return [{"type": text_type, "text": str(content) if content is not None else ""}]

    converted: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            converted.append({"type": text_type, "text": str(block)})
            continue
        if block.get("type") == "text":
            converted.append({"type": text_type, "text": block.get("text", "")})
        elif block.get("type") == "image_url":
            converted.append({"type": "input_image", "image_url": (block.get("image_url") or {}).get("url", "")})
        else:
            converted.append({"type": text_type, "text": str(block)})

    return converted or [{"type": text_type, "text": ""}]
