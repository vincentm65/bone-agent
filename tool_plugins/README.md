# Tool Plugins

This directory is for custom tool plugins that you create. Any `.py` file in this directory will be automatically discovered and loaded when bone-agent starts.

## Quick Start

1. Create a new Python file in this directory (e.g., `my_tool.py`)
2. Use the `@tool` decorator to register your function
3. Run bone-agent - your tool will be available immediately!

## Example

```python
import sys
from pathlib import Path

# Add src to path so we can import tool decorator
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tools.helpers.base import tool

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
    requires_approval=False
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
- `requires_approval: bool` - Whether user confirmation is needed (default: False)
- `tier: str` - Tool tier: `"core"` (always loaded) or `"plugin"` (on-demand, discovered via `search_plugins`)
- `tags: list[str]` - Searchable keywords for plugin discovery (e.g., `["email", "gmail"]`)
- `category: str` - Plugin category for filtering (e.g., `"communication"`, `"data"`)

## Plugin Lifecycle

Plugin-tier tools are loaded on-demand rather than at startup:

1. The LLM calls `search_plugins` with a query to discover available plugins
2. Matching plugins are activated via `ToolRegistry.activate_plugin()`
3. Activated plugins appear in the tool list for subsequent LLM calls
4. Each activation has a TTL — plugins are evicted after a period of inactivity
5. Calling an active plugin resets its TTL (via `touch_plugin`)

## See Also

- `example_tool.py` - More examples
