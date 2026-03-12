"""Result parsing utilities for tool outputs."""

from typing import Optional


def extract_exit_code(tool_result: str) -> Optional[int]:
    """Parse exit_code from tool result string.

    Args:
        tool_result: Tool result content string

    Returns:
        Exit code as integer, or None if not found
    """
    if not isinstance(tool_result, str):
        return None
    first_line = tool_result.splitlines()[0] if tool_result else ""
    if first_line.startswith("exit_code="):
        try:
            value = first_line.split("=", 1)[1].strip()
            value = value.split()[0] if value else value
            return int(value)
        except ValueError:
            return None
    return None


def extract_metadata_from_result(tool_result: str, key: str) -> Optional[int]:
    """Parse metadata like matches_found, lines_read, etc. from tool result.

    Args:
        tool_result: Tool result content string
        key: Metadata key to extract (e.g., "matches_found", "lines_read")

    Returns:
        Extracted value as int, or None if not found
    """
    if not isinstance(tool_result, str):
        return None
    for line in tool_result.split('\n'):
        if line.startswith(f'{key}='):
            try:
                return int(line.split('=')[1].split()[0])
            except (ValueError, IndexError):
                return None
    return None
