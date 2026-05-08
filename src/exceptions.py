"""Custom exception hierarchy for bone-agent."""

class BoneAgentError(Exception):
    """Base exception for all bone-agent application errors.

    All custom exceptions should inherit from this class.
    Provides consistent error handling and allows catching
    all bone-agent-specific errors with a single except clause.
    """
    def __init__(self, message: str, *, details: dict = None):
        """Initialize exception with optional details.

        Args:
            message: Human-readable error message
            details: Optional dictionary with additional error context
        """
        super().__init__(message)
        self.details = details or {}

    def __str__(self):
        base_msg = super().__str__()
        if self.details:
            # Skip multi-line values and internal detail keys from the compact string representation
            # (they're included separately in detailed error formatting)
            single_line_details = {
                k: v for k, v in self.details.items()
                if "\n" not in str(v) and k not in ("original_error",)
            }
            if single_line_details:
                details_str = ", ".join(f"{k}={v}" for k, v in single_line_details.items())
                return f"{base_msg} ({details_str})"
        return base_msg


class ConfigurationError(BoneAgentError):
    """Raised when configuration is invalid, missing, or cannot be loaded."""
    pass


class LLMError(BoneAgentError):
    """Raised when LLM API communication fails or returns unexpected data."""
    pass


class LLMConnectionError(LLMError):
    """Raised when network connection to LLM provider fails."""
    pass


class LLMResponseError(LLMError):
    """Raised when LLM response is malformed or invalid."""
    pass


class ToolExecutionError(BoneAgentError):
    """Raised when tool execution fails."""
    pass


class CommandExecutionError(ToolExecutionError):
    """Raised when shell command execution fails."""
    pass


class FileEditError(ToolExecutionError):
    """Raised when file edit operation fails."""
    pass


class ValidationError(BoneAgentError):
    """Raised when input validation fails."""
    pass


class PathValidationError(ValidationError):
    """Raised when path validation fails (blocked by gitignore, etc.)."""
    pass


