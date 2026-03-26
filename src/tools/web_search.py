"""Web search tool using DuckDuckGo."""

from pathlib import Path
from typing import Optional

from .helpers.base import tool
from utils.web_search import run_web_search
from exceptions import LLMConnectionError


@tool(
    name="web_search",
    description="Search web for info, docs, current events using DuckDuckGo (no API key needed).",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query to execute"
            },
            "num_results": {
                "type": "integer",
                "description": "Results to return (default: 5, max: 10)"
            }
        },
        "required": ["query"]
    },
    allowed_modes=["edit", "plan"],
    requires_approval=False
)
def web_search(
    query: str,
    console,
    num_results: Optional[int] = None
) -> str:
    """Search the web using DuckDuckGo.

    Args:
        query: Search query to execute
        console: Rich console for output (injected by context)
        num_results: Number of results to return (default: 5, max: 10)

    Returns:
        Formatted search results
    """
    arguments = {"query": query}
    if num_results is not None:
        arguments["num_results"] = num_results

    try:
        return run_web_search(arguments, console)
    except LLMConnectionError as e:
        return f"exit_code=1\nWeb search failed: {e}"
    except Exception as e:
        return f"exit_code=1\nWeb search failed: {str(e)}"
