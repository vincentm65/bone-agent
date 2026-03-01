"""Web search using DuckDuckGo (no API key required)."""

from ddgs import DDGS
from exceptions import LLMConnectionError


def run_web_search(arguments, console):
    """Execute web search using DuckDuckGo and return formatted results.

    Args:
        arguments: {
            "query": "search terms to look for",
            "num_results": 5  # optional, number of results (default: 5, max: 10)
        }
        console: Rich console for output

    Returns:
        str: Formatted search results with metadata for model consumption

    Raises:
        LLMConnectionError: If network search fails
    """
    query = arguments.get("query")
    num_results = arguments.get("num_results", 5)

    if not query:
        raise LLMConnectionError(
            "Missing required parameter: query",
            details={"arguments": arguments}
        )

    # Validate and clamp num_results between 1 and 10
    try:
        num_results = max(1, min(10, int(num_results)))
    except (ValueError, TypeError):
        num_results = 5

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=num_results))

        if not results:
            return "results_found=0\nNo results found.\n\n"

        # Format results for model only (not displayed to console)
        output_lines = []
        for idx, result in enumerate(results, 1):
            title = result.get("title", "Untitled")
            url = result.get("href", "N/A")
            body = result.get("body", "No content")

            output_lines.append(f"[{idx}] {title}")
            output_lines.append(f"URL: {url}")
            output_lines.append(f"Snippet: {body}")
            if idx < len(results):
                output_lines.append("")

        # Build result string with metadata for model
        result_content = "\n".join(output_lines)
        return f"results_found={len(results)}\n{result_content}\n\n"

    except LLMConnectionError:
        # Re-raise our custom exceptions
        raise
    except Exception as e:
        console.print(f"Web search failed: {e}", style="red")
        raise LLMConnectionError(
            f"Failed to perform web search",
            details={"query": query, "original_error": str(e)}
        )
