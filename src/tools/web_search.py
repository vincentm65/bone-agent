"""Web search tool using DuckDuckGo."""

from pathlib import Path
from typing import Optional

from .helpers.base import tool
from utils.web_search import run_web_search
from exceptions import LLMConnectionError


@tool(
    name="web_search",
    description="Search web for info, docs, and current events using DuckDuckGo (no API key needed). Automatically fetches and extracts full article content from top results.",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query to execute"
            },
            "num_results": {
                "type": "integer",
                "description": "Results to return (default: 5, max 10)"
            },
            "fetch_content": {
                "type": "boolean",
                "description": "Fetch full page content from top results (default: true). Set to false for URL/snippet only."
            }
        },
        "required": ["query"]
    },
    requires_approval=False
)
def web_search(
    query: str,
    console,
    num_results: Optional[int] = None,
    fetch_content: bool = True
) -> str:
    """Search the web using DuckDuckGo with optional full content extraction.

    Args:
        query: Search query to execute
        console: Rich console for output (injected by context)
        num_results: Number of results to return (default: 5, max: 10)
        fetch_content: Whether to fetch full page content (default: true)

    Returns:
        Formatted search results with content
    """
    arguments = {"query": query}
    if num_results is not None:
        arguments["num_results"] = num_results
    arguments["fetch_content"] = fetch_content

    try:
        return run_web_search(arguments, console)
    except LLMConnectionError as e:
        return f"exit_code=1\nWeb search failed: {e}"
    except Exception as e:
        return f"exit_code=1\nWeb search failed: {str(e)}"
