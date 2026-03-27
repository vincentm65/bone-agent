#!/usr/bin/env python3
"""Test the SettingSelector save flow to identify the bug."""
import sys
sys.path.insert(0, 'src')

from ui.setting_selector import SettingOption, SettingCategory, SettingSelector

# Simulate the exact settings that _open_provider_editor creates
settings = [
    SettingOption(
        key="model", text="Model",
        value="glm-4", input_type="text",
    ),
    SettingOption(
        key="cost_in", text="Cost in",
        value=0.0, input_type="float",
        min_val=0.0, step=0.01,
    ),
    SettingOption(
        key="cost_out", text="Cost out",
        value=0.0, input_type="float",
        min_val=0.0, step=0.01,
    ),
]

# Check initial values snapshot
selector = SettingSelector(
    categories=[SettingCategory(title="Test", settings=settings)],
    title="Test",
)

print("Initial values snapshot:")
for k, v in selector._initial_values.items():
    print(f"  {k}: {v!r} (type={type(v).__name__})")

# Simulate user changing cost_in to 1.5
for cat in selector.categories:
    for s in cat.settings:
        if s.key == "cost_in":
            s.value = float("1.5")
            print(f"\nChanged cost_in to: {s.value!r} (type={type(s.value).__name__})")

# Simulate _save
changes = {}
for cat in selector.categories:
    for setting in cat.settings:
        if setting.value != selector._initial_values.get(setting.key):
            changes[setting.key] = setting.value

print(f"\nChanges dict: {changes}")
print(f"Changes is truthy: {bool(changes)}")

# Now simulate run() return logic
result = changes if changes else {}
print(f"Result: {result}")
print(f"Result is truthy: {bool(result)}")
final = result if result else None
print(f"Final return value: {final}")

# Also test the edge case: cost_in 0.0 -> 0.0 (no change)
print("\n--- Edge case: 0.0 -> 0.0 ---")
selector2 = SettingSelector(
    categories=[SettingCategory(title="Test", settings=[
        SettingOption(key="cost_in", text="Cost in", value=0.0, input_type="float", min_val=0.0, step=0.01),
    ])],
    title="Test",
)
print(f"Initial: {selector2._initial_values['cost_in']!r}")
print(f"Current: {selector2.categories[0].settings[0].value!r}")
print(f"Equal: {selector2.categories[0].settings[0].value == selector2._initial_values['cost_in']}")

# Edge case: cost_in 0.0 -> float("0.0")
print("\n--- Edge case: 0.0 -> float('0.0') ---")
val = float("0.0")
print(f"float('0.0') = {val!r}")
print(f"0.0 == float('0.0'): {0.0 == val}")
