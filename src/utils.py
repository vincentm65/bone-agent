from datetime import datetime, timezone


def format_timestamp(dt: datetime | None = None) -> str:
    """Return an ISO 8601 formatted timestamp string.

    Args:
        dt: A datetime object. Defaults to the current UTC time if not provided.

    Returns:
        ISO 8601 formatted string (e.g. '2026-05-09T12:34:56.789012+00:00').
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.isoformat()
