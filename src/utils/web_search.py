"""Web search using DuckDuckGo (no API key required)."""

import time
import requests
from readability import Document
import html2text

from ddgs import DDGS
from exceptions import LLMConnectionError

# Number of top results to fetch full content from
_DEFAULT_FETCH_COUNT = 3
# Max characters per fetched page to avoid context bloat
_MAX_CONTENT_LENGTH = 8000
# HTTP timeout for page fetching (seconds)
_FETCH_TIMEOUT = 10
# Delay between page fetches to avoid rate limiting (seconds)
_FETCH_DELAY = 1.0
# User agent for page fetching
_USER_AGENT = "Mozilla/5.0 (compatible; bone-agent/1.0; +https://github.com/vincentm65/bone-agent-cli)"


def _strip_invalid_xml_chars(text):
    """Remove characters lxml cannot place in XML/HTML text nodes."""
    return "".join(
        char for char in text
        if (
            char in "\t\n\r"
            or 0x20 <= ord(char) <= 0xD7FF
            or 0xE000 <= ord(char) <= 0xFFFD
            or 0x10000 <= ord(char) <= 0x10FFFF
        )
    )


def _emit_status(text, console, panel_updater=None, style="dim"):
    """Emit status for top-level searches; suppress live sub-agent status.

    Sub-agent calls receive fetch failures through returned tool metadata/errors, so
    live status should not leak into the main chat.

    Args:
        text: Status message (plain text, no Rich markup needed)
        console: Rich console for output (may be None)
        panel_updater: Optional SubAgentPanel indicating sub-agent routing
        style: Rich style name for console output (default: "dim")
    """
    if panel_updater:
        # Sub-agent calls receive page-fetch failures through tool result metadata.
        # Avoid polluting the main chat with informational fetch status.
        return
    if console:
        console.print(f"  [{style}]{text}[/{style}]")


def _fetch_page_content(url):
    """Fetch a URL and extract main article content as markdown.

    Args:
        url: URL to fetch

    Returns:
        tuple: (content, error_reason) where content is the extracted markdown
               (empty string on failure) and error_reason is a short failure
               code like "403", "timeout", "connection error", or None on success.
    """
    try:
        response = requests.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=_FETCH_TIMEOUT,
            allow_redirects=True
        )
        response.raise_for_status()

        # Skip non-HTML content (PDFs, images, JSON APIs, etc.)
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            return "", "non-HTML"

        # Check for empty response before parsing
        page_text = _strip_invalid_xml_chars(response.text)
        if not page_text or not page_text.strip():
            return "", "empty"

        # Use readability to extract the main article content
        doc = Document(page_text)
        summary_html = doc.summary()

        # Convert cleaned HTML to markdown (per-call instance for thread safety)
        md = html2text.HTML2Text()
        md.ignore_links = False
        md.ignore_images = True
        md.body_width = 0
        content = md.handle(summary_html).strip()

        # Truncate at last newline/whitespace before limit to avoid mid-word splits
        if len(content) > _MAX_CONTENT_LENGTH:
            cutoff = content.rfind("\n", 0, _MAX_CONTENT_LENGTH)
            if cutoff < _MAX_CONTENT_LENGTH * 0.8:
                cutoff = _MAX_CONTENT_LENGTH
            content = content[:cutoff] + "\n\n[... content truncated]"

        return content, None

    except requests.HTTPError as e:
        status = e.response.status_code
        return "", str(status)
    except requests.Timeout:
        return "", "timeout"
    except requests.ConnectionError:
        return "", "connection error"
    except requests.RequestException:
        return "", "request error"
    except (ValueError, TypeError, UnicodeError):
        return "", "parse error"
    except Exception:
        return "", "parse error"


def run_web_search(arguments, console, panel_updater=None):
    """Execute web search using DuckDuckGo and return formatted results.

    Args:
        arguments: {
            "query": "search terms to look for",
            "num_results": 5,  # optional, number of results (default: 5, max: 10)
            "fetch_content": true  # optional, fetch full page content (default: true)
        }
        console: Rich console for output
        panel_updater: Optional SubAgentPanel for routing output to sub-agent panel

    Returns:
        str: Formatted search results with metadata for model consumption

    Raises:
        LLMConnectionError: If network search fails
    """
    query = arguments.get("query")
    num_results = arguments.get("num_results", 5)
    fetch_content = arguments.get("fetch_content", True)

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

        # Determine how many results to fetch content from
        fetch_count = min(_DEFAULT_FETCH_COUNT, len(results)) if fetch_content else 0
        pages_fetched = 0
        pages_failed = 0
        failure_reasons = []  # Collect short failure codes for metadata

        # Format results for model
        output_lines = []
        for idx, result in enumerate(results, 1):
            title = result.get("title", "Untitled")
            url = result.get("href", "N/A")
            body = result.get("body", "No content")

            output_lines.append(f"[{idx}] {title}")
            output_lines.append(f"URL: {url}")
            output_lines.append(f"Snippet: {body}")

            # Fetch full content for top results
            if fetch_content and idx <= fetch_count:
                content, error_reason = _fetch_page_content(url)
                if content:
                    output_lines.append(f"\n--- Content ---\n{content}")
                    pages_fetched += 1
                else:
                    output_lines.append(f"\n[Failed to fetch page content]")
                    pages_failed += 1
                    if error_reason:
                        failure_reasons.append(error_reason)
                        # Emit live status only for top-level searches; sub-agents
                        # receive this through the returned metadata below.
                        _emit_status(f"Failed: {error_reason}", console, panel_updater)

                # Rate limiting: delay between fetches
                if idx < fetch_count:
                    time.sleep(_FETCH_DELAY)

            if idx < len(results):
                output_lines.append("")

        # Build result string with metadata for model
        result_content = "\n".join(output_lines)
        meta = f"results_found={len(results)}"
        if fetch_content:
            meta += f", pages_fetched={pages_fetched}"
            if pages_failed:
                meta += f", pages_failed={pages_failed}"
            if failure_reasons:
                meta += f", failures={','.join(failure_reasons)}"
        return f"{meta}\n{result_content}\n\n"

    except LLMConnectionError:
        # Re-raise our custom exceptions
        raise
    except Exception as e:
        _emit_status(f"Web search failed: {e}", console, panel_updater, style="red")
        raise LLMConnectionError(
            f"Failed to perform web search",
            details={"query": query, "original_error": str(e)}
        )
