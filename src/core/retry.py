"""Retry logic for LLM connection and timeout errors."""

import time

from exceptions import LLMResponseError

# Timeout retry constants
RETRY_MAX_ATTEMPTS = 3
RETRY_DELAYS = (2, 4)  # exponential backoff per attempt
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
# Full keyword list for errors without HTTP status codes (network-level exceptions)
RETRYABLE_ERROR_KEYWORDS = (
    "timeout", "timed out", "connectionerror", "connection refused",
    "connection reset", "connection aborted", "name or service not known",
    "network error", "network unreachable", "no route to host", "eof occurred",
)
# Stricter keyword list for errors that DO have an unrecognized status code.
# Excludes generic phrases like "network error" that may appear in arbitrary
# HTTP response bodies, while keeping transport-level signals.
RETRYABLE_STATUS_ERROR_KEYWORDS = (
    "timeout", "timed out", "connectionerror", "connection refused",
    "connection reset", "connection aborted", "name or service not known",
    "network unreachable", "no route to host", "eof occurred",
)
NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 405, 422}


def is_retryable_error(error):
    """Check if an LLMConnectionError is retryable.

    Decision order:
    1. LLMResponseError → never retryable (parsing failure).
    2. Explicit HTTP status code:
       - Non-retryable set (400, 401, 403, 405, 422) → False.
       - Retryable set (429, 500, 502, 503, 504) → True.
    3. Unrecognized status code: keyword-match against the stricter
       RETRYABLE_STATUS_ERROR_KEYWORDS to avoid false positives from
       generic phrases like "network error" in response bodies.
    4. No status code (pure network/transport error): keyword-match
       against the full RETRYABLE_ERROR_KEYWORDS.

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
        # Unrecognized status code: use stricter keywords to avoid
        # false positives from generic phrases in response bodies.
        keywords = RETRYABLE_STATUS_ERROR_KEYWORDS
    else:
        # No status code: likely a transport-level error; use full keywords.
        keywords = RETRYABLE_ERROR_KEYWORDS

    # For network-level errors, check the original error message
    original_error = details.get("original_error", "")
    original_lower = original_error.lower()
    return any(keyword in original_lower for keyword in keywords)


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
