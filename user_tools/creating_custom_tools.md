# Creating Custom Tools

This guide explains how to create custom tools for vmCode using the new decorator-based API.

## Table of Contents

- [Quick Start](#quick-start)
- [Tool Decorator Reference](#tool-decorator-reference)
- [Parameter Types and Validation](#parameter-types-and-validation)
- [Accessing Context](#accessing-context)
- [Error Handling and Result Formatting](#error-handling-and-result-formatting)
- [Testing Custom Tools](#testing-custom-tools)
- [Best Practices](#best-practices)

---

## Quick Start

Create a custom tool in 3 simple steps:

### Step 1: Create a Python File

Create a new file in the `tools/` directory:

```bash
touch tools/my_custom_tool.py
```

### Step 2: Add Your Tool

```python
import sys
from pathlib import Path

# Add src to path so we can import tool decorator
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tools.base import tool  # or: from tools.helpers.base import tool

@tool(
    name="my_tool",
    description="Does something useful",
    parameters={
        "type": "object",
        "properties": {
            "input": {"type": "string", "description": "Input value"}
        },
        "required": ["input"]
    }
)
def my_tool(input: str, repo_root: Path) -> str:
    """Your tool implementation."""
    result = process(input)
    return f"exit_code=0\n{result}"
```

### Step 3: Use It

Run vmCode and your tool is immediately available:

```bash
vmcode
> Help me process this file
# LLM can now use my_tool
```

---

## Tool Decorator Reference

### Basic Syntax

```python
@tool(
    name="tool_name",
    description="Tool description for LLM",
    parameters={
        "type": "object",
        "properties": {...},
        "required": [...]
    },
    allowed_modes=["edit", "plan", "learn"],
    requires_approval=False
)
def tool_function(param1: type, param2: type, ...) -> str:
    """Tool implementation."""
    return f"exit_code=0\nResult"
```

### Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `name` | `str` | Yes | - | Tool identifier (unique across all tools) |
| `description` | `str` | Yes | - | Description shown to LLM |
| `parameters` | `dict` | Yes | - | JSON Schema for function parameters |
| `allowed_modes` | `list[str]` | No | `["edit", "plan", "learn"]` | Modes where tool is available |
| `requires_approval` | `bool` | No | `False` | Whether tool needs user confirmation |

### Parameter Schema (JSON Schema)

The `parameters` dict follows JSON Schema format:

```python
parameters={
    "type": "object",
    "properties": {
        "param1": {
            "type": "string",
            "description": "Description for parameter"
        },
        "param2": {
            "type": "integer",
            "description": "Optional parameter"
        },
        "param3": {
            "type": "boolean",
            "description": "Flag parameter",
            "default": false
        }
    },
    "required": ["param1"]
}
```

#### Supported Types

- `"string"` - Text values
- `"integer"` - Whole numbers
- `"number"` - Floating point numbers
- `"boolean"` - True/false values
- `"array"` - Lists (specify `items` type)
- `"object"` - Nested objects

---

## Parameter Types and Validation

### String Parameters

```python
@tool(
    name="greet",
    description="Greet a person",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Person's name"}
        },
        "required": ["name"]
    }
)
def greet(name: str) -> str:
    return f"exit_code=0\nHello, {name}!"
```

### Integer Parameters

```python
@tool(
    name="count_lines",
    description="Count lines in a file",
    parameters={
        "type": "object",
        "properties": {
            "max_lines": {
                "type": "integer",
                "description": "Maximum lines to count",
                "default": 100
            }
        },
        "required": []
    }
)
def count_lines(max_lines: int = 100) -> str:
    return f"exit_code=0\nCounting up to {max_lines} lines"
```

### Boolean Parameters

```python
@tool(
    name="recursive_search",
    description="Search recursively",
    parameters={
        "type": "object",
        "properties": {
            "recursive": {
                "type": "boolean",
                "description": "Search recursively",
                "default": False
            }
        },
        "required": []
    }
)
def recursive_search(recursive: bool = False) -> str:
    return f"exit_code=0\nRecursive: {recursive}"
```

### Array Parameters

```python
@tool(
    name="process_files",
    description="Process multiple files",
    parameters={
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of file paths"
            }
        },
        "required": ["files"]
    }
)
def process_files(files: list[str]) -> str:
    return f"exit_code=0\nProcessing {len(files)} files"
```

---

## Accessing Context

Your tool functions can receive context parameters automatically injected by the framework:

### Available Context Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `repo_root` | `Path` | Repository root directory |
| `console` | `Console` | Rich console for output |
| `gitignore_spec` | `PathSpec` | .gitignore filter |
| `debug_mode` | `bool` | Debug mode enabled |
| `interaction_mode` | `str` | Current mode ("edit", "plan", "learn") |

### Example: Using repo_root

```python
@tool(
    name="list_readme",
    description="List README files",
    parameters={
        "type": "object",
        "properties": {},
        "required": []
    }
)
def list_readme(repo_root: Path) -> str:
    """Find all README files in repository."""
    readmes = list(repo_root.rglob("README*"))
    readme_list = "\n".join(f"  - {r.relative_to(repo_root)}" for r in readmes)
    return f"exit_code=0\nFound {len(readmes)} README files:\n{readme_list}"
```

### Example: Using gitignore_spec

```python
from tools.file_helpers import _is_fast_ignored, _is_ignored_cached, _register_gitignore_spec  # or: from tools.helpers.file_helpers import ...

@tool(
    name="count_non_ignored_files",
    description="Count files not ignored by gitignore",
    parameters={
        "type": "object",
        "properties": {},
        "required": []
    }
)
def count_non_ignored_files(repo_root: Path, gitignore_spec) -> str:
    """Count files not in .gitignore."""
    spec_key = _register_gitignore_spec(gitignore_spec)
    count = 0

    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        if not _is_fast_ignored(path) and not _is_ignored_cached(str(path), str(repo_root), spec_key):
            count += 1

    return f"exit_code=0\nFound {count} non-ignored files"
```

### Example: Using console

```python
@tool(
    name="show_progress",
    description="Show progress during operation",
    parameters={
        "type": "object",
        "properties": {
            "count": {"type": "integer", "description": "Number of items"}
        },
        "required": ["count"]
    }
)
def show_progress(count: int, console) -> str:
    """Display progress to user."""
    for i in range(count):
        console.print(f"Processing item {i+1}/{count}...", style="dim")

    return f"exit_code=0\nProcessed {count} items"
```

---

## Error Handling and Result Formatting

### Return Format

All tools must return a string with an exit code prefix:

```python
# Success
return "exit_code=0\nOperation completed successfully"

# Error
return "exit_code=1\nError: Something went wrong"
```

### Handling Exceptions

```python
@tool(
    name="safe_file_read",
    description="Safely read a file",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path"}
        },
        "required": ["path"]
    }
)
def safe_file_read(path: str, repo_root: Path) -> str:
    """Read a file with error handling."""
    try:
        file_path = repo_root / path
        if not file_path.exists():
            return f"exit_code=1\nError: File not found: {path}"

        content = file_path.read_text(encoding="utf-8")
        return f"exit_code=0\n{content}"

    except PermissionError:
        return f"exit_code=1\nError: Permission denied: {path}"
    except Exception as e:
        return f"exit_code=1\nError: {str(e)}"
```

### Validation Examples

```python
@tool(
    name="validate_email",
    description="Validate an email address",
    parameters={
        "type": "object",
        "properties": {
            "email": {"type": "string", "description": "Email to validate"}
        },
        "required": ["email"]
    }
)
def validate_email(email: str) -> str:
    """Validate email format."""
    import re

    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    if re.match(pattern, email):
        return f"exit_code=0\nValid email: {email}"
    else:
        return f"exit_code=1\nInvalid email format: {email}"
```

---

## Testing Custom Tools

### Unit Testing

Create a test file `tests/test_my_tool.py`:

```python
import pytest
from pathlib import Path
from tools.base import ToolRegistry, tool

def test_my_tool_basic():
    """Test basic tool functionality."""

    @tool(
        name="test_tool",
        description="Test",
        parameters={"type": "object", "properties": {}, "required": []}
    )
    def test_tool(input: str) -> str:
        return f"exit_code=0\n{input}"

    tool_def = ToolRegistry.get("test_tool")
    assert tool_def is not None

    result = tool_def.execute({"input": "hello"}, {})
    assert result == "exit_code=0\nhello"
```

### Manual Testing

1. Add your tool to `tools/` directory
2. Run vmCode: `vmcode`
3. Test via conversation:
   ```
   > Use my_tool with input "test"
   ```

---

## Best Practices

### 1. Descriptive Names and Descriptions

```python
# Good
@tool(
    name="count_python_lines",
    description="Count lines of code in Python files"
)

# Avoid
@tool(
    name="cnt",  # Too cryptic
    description="Do stuff"  # Too vague
)
```

### 2. Type Hints

Always use type hints for parameters:

```python
def my_tool(file_path: str, recursive: bool = False, repo_root: Path = None) -> str:
    ...
```

### 3. Return Exit Codes

Always include exit code in result:

```python
# Good
return f"exit_code=0\nSuccess"

# Bad - missing exit code
return "Success"
```

### 4. Handle Errors Gracefully

```python
def robust_tool(path: str, repo_root: Path) -> str:
    try:
        # Implementation
        return f"exit_code=0\n{result}"
    except FileNotFoundError:
        return f"exit_code=1\nError: File not found: {path}"
    except Exception as e:
        return f"exit_code=1\nError: {str(e)}"
```

### 5. Respect Gitignore

When working with files, respect `.gitignore`:

```python
from tools.file_helpers import _is_fast_ignored, _is_ignored_cached, _register_gitignore_spec  # or: from tools.helpers.file_helpers import ...

spec_key = _register_gitignore_spec(gitignore_spec)
if _is_fast_ignored(path) or _is_ignored_cached(str(path), str(repo_root), spec_key):
    return f"exit_code=1\nError: File is gitignored"
```

### 6. Use Appropriate Modes

Only allow tools in appropriate modes:

```python
# Write-only tool - only in edit mode
@tool(
    name="modify_config",
    allowed_modes=["edit"]
)
def modify_config(...) -> str:
    ...

# Read-only tool - all modes
@tool(
    name="read_log",
    allowed_modes=["edit", "plan", "learn"]
)
def read_log(...) -> str:
    ...
```

### 7. Document Your Tool

Use docstrings to explain your tool:

```python
@tool(...)
def my_complex_tool(param1: str, param2: int = 10) -> str:
    """
    Process data with multiple steps.

    Steps:
    1. Validate input
    2. Process data
    3. Return result

    Args:
        param1: Primary input string
        param2: Optional limit (default: 10)

    Returns:
        Formatted result with exit_code prefix
    """
    ...
```

---

## Troubleshooting

### Tool Not Showing Up

- Check file is in `src/tools/` or `user_tools/`
- Ensure file ends with `.py` extension
- Verify `@tool` decorator is applied
- Check for syntax errors (vmCode logs these on startup)

### Import Errors

- Make sure `sys.path.insert(0, ...)` is at top of file
- Check that `src` directory path is correct
- Verify no naming conflicts with other tools

### Runtime Errors

- Check return format includes `exit_code=N` prefix
- Ensure all required parameters are in JSON Schema
- Verify function signature matches declared parameters

---

## Examples

See `tools/example_tool.py` for more complete examples including:
- Counting lines in Python files
- Finding empty files
- Using gitignore filtering
- Error handling patterns
