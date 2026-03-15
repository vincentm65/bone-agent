"""Centralized constants for tools.

This module contains all magic numbers and configuration values used across
the tools infrastructure. Centralizing constants makes the code more
maintainable and self-documenting.
"""

# ============================================================================
# Directory Listing Constants
# ============================================================================

# Total items that trigger truncation in directory listings
TRUNCATION_THRESHOLD = 100

# Maximum files to show per folder when truncating directory listings
MAX_FILES_PER_FOLDER = 10

# Hard upper limit for total items to collect in directory listings
# Prevents context explosion on very large directories
MAX_TOTAL_ITEMS = 500


# ============================================================================
# File Reading Constants
# ============================================================================

# Chunk size for streaming file reads (8KB)
# Balances memory usage with read performance
FILE_READ_CHUNK_SIZE = 8192

# Maximum buffer size for file reading (10MB)
# Handles pathological files with very long single lines
FILE_READ_MAX_BUFFER_SIZE = 10_000_000

# Maximum lines to show in file output formatting
# Prevents overwhelming context with excessive output
FORMATTER_MAX_LINES = 100


# ============================================================================
# Task List Constants
# ============================================================================

# Maximum number of tasks allowed in a task list
MAX_TASKS = 50

# Maximum length for individual task descriptions
MAX_TASK_LEN = 200

# Maximum length for task list titles
MAX_TASK_TITLE_LEN = 80


# ============================================================================
# UI/Display Constants
# ============================================================================

# Default terminal width fallback for non-TTY environments
DEFAULT_TERMINAL_WIDTH = 80
