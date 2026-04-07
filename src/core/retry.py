"""Retry logic for LLM connection and timeout errors."""

import time

from exceptions import LLMResponseError

# Timeout retry constants
RETRY_MAX_ATTEMPTS = 3
RETRY_DELAYS = (2, 4)  # exponential backoff per attempt
RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
RETRYABLE_ERROR_KEYWORDS = (
    "timeout", "timed out", "connectionerror", "connection refused",
    "connection reset", "connection aborted", "name or service not known",
    "network unreachable", "no route to host", "eof occurred",
)
NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 405, 422}


def is_retryable_error(error):
    """Check if an LLMConnectionError is retryable.

    Retryable conditions:
    - Timeout or connection-level errors (network unreachable, DNS failure, etc.)
    - HTTP 429 (rate limited), 502, 503, 504 (server errors)

    Non-retryable conditions:
    - HTTP 400, 401, 403, 405, 422 (client/auth errors)
    - LLMResponseError (malformed response data)

    Args:
        error: Exception instance (typically LLMConnectionError)

    Returns:
        bool: True if the error is retryable
    """
    # Never retry response parsing errors
    if isinstance(error, LLMResponseError):
        return False

    # Check HTTP status code first (most reliable signal)
    details = getattr(error, 'details', {}) or {}
    status_code = details.get("status_code")
    if status_code is not None:
        if status_code in NON_RETRYABLE_STATUS_CODES:
            return False
        if status_code in RETRYABLE_STATUS_CODES:
            return True

    # For network-level errors, check the original error message
    original_error = details.get("original_error", "")
    original_lower = original_error.lower()
    return any(keyword in original_lower for keyword in RETRYABLE_ERROR_KEYWORDS)


def wait_with_cancel_message(console, delay_seconds):
    """Wait briefly before retrying, showing a dim status line.

    Args:
        console: Rich console for output
        delay_seconds: Seconds to wait

    Returns:
        bool: True if wait completed, False if interrupted by KeyboardInterrupt
    """
    console.print(f"[dim]Connection issue, retrying in {delay_seconds}s... (Ctrl+C to cancel)[/dim]")
    try:
        time.sleep(delay_seconds)
    except KeyboardInterrupt:
        console.print("[dim]Retry cancelled.[/dim]")
        return False
    return True
