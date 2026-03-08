"""Test script to verify select_option tool after fix."""

import sys
sys.path.insert(0, 'src')

from tools import ToolRegistry

# Check tool registration
tool = ToolRegistry.get('select_option')
print(f"Tool found: {tool is not None}")
if tool:
    print(f"Tool name: {tool.name}")
    print(f"Allowed modes: {tool.allowed_modes}")
    print(f"Requires approval: {tool.requires_approval}")
    print(f"Handler: {tool.handler}")

    # Try to import show_option_menu to verify it works
    try:
        from ui.option_menu import show_option_menu
        print("✓ show_option_menu imported successfully")
    except ImportError as e:
        print(f"✗ Failed to import show_option_menu: {e}")
else:
    print("Tool not found in registry!")
