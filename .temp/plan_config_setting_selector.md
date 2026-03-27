# Plan: Convert `/config` Runtime Settings to Use SettingSelector

## Goal
Replace the read-only `show_config_overview()` call in `/config` with an interactive `SettingSelector` that lets users view and edit runtime settings in one place.

## Current State
- `/config` calls `show_config_overview()` in `src/ui/displays.py:88-162` — purely displays two Rich tables (Runtime Settings + Provider Settings)
- Runtime settings (debug, logging, interaction mode, approval mode) are changed via separate commands: `/debug`, `/logging`, `/mode`
- No dedicated command exists for approval mode at runtime (only cycled during edit approvals)
- `SettingSelector` (`src/ui/setting_selector.py`) supports: `boolean`, `nav`, `select`, `text`, `number`, `float` input types

## Do We Need to Change `setting_selector.py`?

**No.** The existing `SettingSelector` already supports everything we need:

| Setting | Input Type | Notes |
|---|---|---|
| Debug Mode | `boolean` | Toggle on/off |
| Conversation Logging | `boolean` | Toggle on/off |
| Interaction Mode | `select` | Options: `edit`, `plan` |
| Approval Mode | `select` | Options: `safe`, `normal`, `danger` |

No new input types ("Edit" or "Safe") are needed. These are just option values within the existing `select` type.

## Changes Required

### 1. `src/ui/commands.py` — Replace `_handle_config` (lines 120-124)

Replace the current handler that calls `show_config_overview()` with a new implementation that:
- Builds `SettingOption` objects for the 4 runtime settings using current values from `chat_manager` and `debug_mode_container`
- Groups them in a single `SettingCategory`
- Creates and runs a `SettingSelector` instance
- Applies any changes back to `chat_manager` / `debug_mode_container`
- Displays a summary of changes

The new handler will:
```
debug_mode → boolean toggle → debug_mode_container['debug']
logging   → boolean toggle → chat_manager.toggle_logging() (or direct set)
mode      → select [edit, plan] → chat_manager.interaction_mode
approve   → select [safe, normal, danger] → chat_manager.approve_mode
```

### 2. `src/ui/displays.py` — Keep `show_config_overview()` (no change)

This function remains available as a read-only reference display. It is not used by the new interactive `/config`, but could be useful for `/help` context or future use. No changes needed.

### 3. `src/core/chat_manager.py` — Add setter for interaction mode (if missing)

Check if there's a `set_interaction_mode()` method. If not, add one so the selector callback can set the mode directly (currently only `toggle_interaction_mode()` exists). Similarly, verify logging can be set directly or needs a new setter.

## Implementation Notes

- The `on_change` callback on `SettingSelector` can be used for immediate feedback as values change
- After saving, `display_startup_banner()` should be reprinted to refresh the banner with new mode/approve settings (consistent with how `/provider` and `/sb` work)
- Boolean toggles (debug, logging) will flip on Enter — no separate edit mode needed
- Select types (mode, approve) will use arrow keys to cycle options
- Provider settings should NOT be included in this selector (that's what `/provider` is for)
