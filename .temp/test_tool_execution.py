"""Test the select_option tool execution directly."""

import sys
sys.path.insert(0, 'src')

from rich.console import Console
from tools.option_menu import select_option

console = Console()

# Test 1: Valid call (will show menu)
print("Test 1: Valid call with menu")
print("=" * 50)
result = select_option(
    question="Which database do you want to use?",
    options=[
        {"value": "postgresql", "text": "PostgreSQL", "description": "Production-ready"},
        {"value": "sqlite", "text": "SQLite", "description": "Lightweight"}
    ],
    console=console,
    title="Database Selection"
)
print(f"Result: {repr(result)}")
print()

# Test 2: Invalid options (should return error without showing menu)
print("Test 2: Invalid options (missing 'value' key)")
print("=" * 50)
result = select_option(
    question="Which database?",
    options=[
        {"text": "PostgreSQL"},  # Missing 'value' key
    ],
    console=console,
    title="Test"
)
print(f"Result: {repr(result)}")
print()

# Test 3: Empty options list (should return error)
print("Test 3: Empty options list")
print("=" * 50)
result = select_option(
    question="Which database?",
    options=[],
    console=console,
    title="Test"
)
print(f"Result: {repr(result)}")
print()

print("All tests completed!")
