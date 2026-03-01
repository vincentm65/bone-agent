"""Shared type conversion utilities for tool argument validation."""


def coerce_int(value):
    """Best-effort coercion of tool arguments to int.

    Returns:
        Tuple of (int_value, error_message). error_message is None on success.
    """
    if value is None:
        return None, "Missing required integer value."
    if isinstance(value, bool):
        return None, "Value must be an integer, not a boolean."
    if isinstance(value, int):
        return value, None
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return None, "Value must be a non-empty integer."
        try:
            return int(text), None
        except ValueError:
            return None, "Value must be an integer."
    return None, "Value must be an integer."


def coerce_bool(value, default=None):
    """Best-effort coercion of tool arguments to boolean.

    Returns None if value is None and default is None.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off"}:
            return False
    return default
