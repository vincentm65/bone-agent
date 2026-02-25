"""Tool definitions for OpenAI-style function calling.

This module contains the schema definitions for all available tools,
and utilities to filter them based on interaction mode.
"""

# Tool definition for OpenAI-style function calling
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "rg",
            "description": "A powerful search tool built on ripgrep\n\n**Usage:**\n- ALWAYS use rg for search tasks. NEVER invoke `grep` or `rg` as a shell command. The rg tool has been optimized for correct permissions and access.\n- Supports full regex syntax (e.g., \"log.*Error\", \"function\\s+\\w+\")\n- Filter files with glob parameter (e.g., \"*.js\", \"**/*.tsx\") or type parameter (e.g., \"js\", \"py\", \"rust\")\n- Output modes: \"content\" shows matching lines, \"files_with_matches\" shows only file paths (default), \"count\" shows match counts\n- Use sub_agent tool for open-ended searches requiring multiple rounds\n- Pattern syntax: Uses ripgrep (not grep) - literal braces need escaping (use `interface\\{\\}` to find `interface{}` in Go code)\n- Multiline matching: By default patterns match within single lines only. For cross-line patterns like `struct \\{[\\s\\S]*?field`, use `multiline: true`",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "The regular expression pattern to search for in file contents"},
                    "path": {"type": "string", "description": "File or directory to search in (rg PATH). Defaults to current working directory."},
                    "glob": {"type": "string", "description": "Glob pattern to filter files (e.g. \"*.js\", \"*.{ts,tsx}\") - maps to rg --glob"},
                    "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"], "description": "Output mode: \"content\" shows matching lines (supports -B/-A/-C context, -n line numbers), \"files_with_matches\" shows file paths, \"count\" shows match counts. Defaults to \"files_with_matches\"."},
                    "-B": {"type": "number", "description": "Number of lines to show before each match (rg -B). Requires output_mode: \"content\", ignored otherwise."},
                    "-A": {"type": "number", "description": "Number of lines to show after each match (rg -A). Requires output_mode: \"content\", ignored otherwise."},
                    "-C": {"type": "number", "description": "Number of lines to show before and after each match (rg -C). Requires output_mode: \"content\", ignored otherwise."},
                    "-n": {"type": "boolean", "description": "Show line numbers in output (rg -n). Requires output_mode: \"content\", ignored otherwise."},
                    "-i": {"type": "boolean", "description": "Case insensitive search (rg -i)"},
                    "type": {"type": "string", "description": "File type to search (rg --type). Common types: js, py, rust, go, java, etc. More efficient than include for standard file types."},
                    "multiline": {"type": "boolean", "description": "Enable multiline mode where . matches newlines and patterns can span lines (rg -U --multiline-dotall). Default: false."}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": "Execute shell commands for git, system tasks, debugging, and file operations.\n\n**Use for:**\n- Git operations: git clone, pull, push, status, etc.\n- System debugging: ps, lsof, netstat, journalctl, systemctl\n- File operations: rm, mv, cp, mkdir (system-wide)\n- Network tools: ping, curl, wget, ssh\n- Package management: pacman, pip, npm, apt\n- Path navigation: cd /path && command (use && for chaining)\n\n**Important:**\n- All commands execute from repository root\n- Use && for conditional chaining (stops on error)\n- Absolute paths allowed for system debugging\n- Commands outside repo show warnings but execute\n\n**Do NOT use for:**\n- Code search (use rg tool)\n- Reading codebase files (use read_file)\n- Listing codebase directories (use list_directory)\n- Creating/editing codebase files (use create_file/edit_file)\n- NO chaining with ;, |, >, <, ` (only && allowed)",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command to execute. Examples: 'git status', 'ps aux', 'cd /var/log && tail -f syslog'"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read file contents using Python file reader. Use this to view a file (or a specific line range). Prefer this over rg when you already know the file path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to read (relative to repo root)"},
                    "max_lines": {"type": "integer", "description": "Max lines to read (omit for full file)"},
                    "start_line": {"type": "integer", "description": "1-based starting line number (default: 1). Use with max_lines to read a specific excerpt."}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List directory contents using Python file lister (preferred over PowerShell).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to list (relative to repo root, default: '.')"},
                    "recursive": {"type": "boolean", "description": "List recursively (default: false)"},
                    "show_files": {"type": "boolean", "description": "Include files (default: true)"},
                    "show_dirs": {"type": "boolean", "description": "Include directories (default: true)"},
                    "pattern": {"type": "string", "description": "Glob pattern to filter results (e.g., \"*.py\")"}
                },
                "required": ["path"]
            }
        }
    },
        {
            "type": "function",
            "function": {
                "name": "create_file",
                "description": "Create a new file with optional initial content. File must not exist. For small files, include content directly. Creates a preview of the written content (up to 200 lines) with syntax highlighting.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to create (relative to repo root)"},
                        "content": {"type": "string", "description": "Initial content (omit for empty file)"}
                    },
                    "required": ["path"]
                }
            }
        },    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Apply search/replace edit to file. Search text must appear exactly once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to edit (relative to repo root)"},
                    "search": {"type": "string", "description": "Exact text to find. Must be unique. Include context. Multi-line supported."},
                    "replace": {"type": "string", "description": "Replacement text. Multi-line supported."},
                    "context_lines": {"type": "integer", "description": "Context lines in diff (default: 3)"},
                    "color": {"type": "string", "description": "Color mode: 'auto', 'on', 'off' (default: 'auto')"}
                },
                "required": ["path", "search", "replace"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search web for info, docs, current events using DuckDuckGo (no API key needed).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query to execute"},
                    "num_results": {"type": "integer", "description": "Results to return (default: 1, max: 5)"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "sub_agent",
            "description": "MANDATORY: MUST CALL THIS FIRST before ANY rg or read_file when answering: 'how something works', architecture, patterns, multi-file flows, or broad exploration. DO NOT search manually - this tool is 10x faster. Examples: 'How does authentication work?', 'Explain the data flow', 'Where is X handled?'",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Task query, e.g. 'How does the chat manager handle history?'"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_task_list",
            "description": "Create or replace an in-session task list for tracking long EDIT workflows.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {"type": "array", "items": {"type": "string"}, "description": "Task descriptions. Non-empty after trimming."},
                    "title": {"type": "string", "description": "Optional short title for the task list."}
                },
                "required": ["tasks"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": "Mark one or more tasks complete in the current in-session task list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "Zero-based index of a single task to mark complete."},
                    "task_ids": {"type": "array", "items": {"type": "integer"}, "description": "Array of zero-based task indices to mark complete."}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "show_task_list",
            "description": "Show the current in-session task list without modifying it.",
            "parameters": {"type": "object", "properties": {}}
        }
    }
]


def _tools_for_mode(interaction_mode):
    """Filter tools based on interaction mode.

    Args:
        interaction_mode: 'plan', 'edit', or 'learn'

    Returns:
        List of tool definitions suitable for the mode
    """
    if interaction_mode in ("learn", "plan"):
        allowed = {"rg", "read_file", "list_directory", "sub_agent", "web_search"}
        return [tool for tool in TOOLS if tool["function"]["name"] in allowed]
    return TOOLS
