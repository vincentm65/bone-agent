# User Custom Tools

This directory is for custom tools that you create. Any `.py` file in this directory will be automatically discovered and loaded when vmCode starts.

## Quick Start

1. Create a new Python file in this directory (e.g., `my_tool.py`)
2. Use the `@tool` decorator to register your function
3. Run vmCode - your tool will be available immediately!

## Example

```python
import sys
from pathlib import Path

# Add src to path so we can import tool decorator
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tools.base import tool

@tool(
    name="my_tool",
    description="Does something useful",
    parameters={
        "type": "object",
        "properties": {
            "input": {"type": "string", "description": "Input value"}
        },
        "required": ["input"]
    },
    allowed_modes=["edit"]
)
def my_tool(input: str, repo_root: Path) -> str:
    """Your tool implementation."""
    result = do_something(input)
    return f"exit_code=0\n{result}"
```

## Available Context Parameters

Your tool function can accept these parameters (they'll be injected automatically):

- `repo_root: Path` - Repository root directory
- `console` - Rich console for output
- `gitignore_spec` - PathSpec for .gitignore filtering
- `debug_mode: bool` - Whether debug mode is enabled
- `interaction_mode: str` - Current interaction mode ("edit", "plan", or "learn")

## Return Format

Tools must return a string with an exit code prefix:

```python
return f"exit_code=0\nSuccess message here"
# or
return f"exit_code=1\nError: Something went wrong"
```

## Decorator Parameters

- `name: str` - Tool identifier (used by LLM)
- `description: str` - Description for LLM
- `parameters: dict` - JSON Schema for parameters
- `allowed_modes: list` - Modes where tool is available (default: all modes)
- `requires_approval: bool` - Whether user confirmation is needed (default: False)

## See Also

- `example_tool.py` - More examples
- `creating_custom_tools.md` - Complete documentation
