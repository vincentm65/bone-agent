# Review: Settings Selector (`src/ui/setting_selector.py`)

## Dead Code

1. **`_total_setting_rows()`** (`setting_selector.py:117`) — Defined but never called anywhere. No consumer in `commands.py` or any other file uses it. The navigation logic (`_navigate_down`/`_navigate_up`) works with category/setting indices directly.

2. **`select_setting()`** (`setting_selector.py:545-583`) — The "convenience function for quick usage" is never called anywhere. All three call sites in `commands.py` construct `SettingSelector` directly with `SettingCategory` lists. This function wraps single-option selection but is dead code.

3. **`ConfigManager.delete_model_price()`** (`config_manager.py:233`) — Only called in `.temp/test_save.py` (a manual test script), never in production code.

4. **`ConfigManager.list_model_prices()`** (`config_manager.py:224`) — Zero call sites anywhere.

5. **`validate_fn` on `SettingOption`** (`setting_selector.py:24`) — The field exists and `_validate_input` handles it, but no caller in `commands.py` ever passes a `validate_fn`. It's infrastructure for an unused feature.

6. **`step` parameter** — Only used for `float` settings with `step=0.01` in the provider editor. The `number` type's step validation exists but is never exercised (no `number` type settings are ever created with `step`).

## Architecture Issues

1. **`_get_display_text()` is a 100-line monolithic rendering method** (`setting_selector.py:131-269`). It branches on `input_type` (boolean, nav, select, text, number, float) with duplicated patterns for selected vs unselected, editing vs non-editing states. There are repeated `f"> <b>{label}:</b>..."` blocks that differ only in styling. This should be broken into per-type render helpers or use a dispatch pattern.

2. **Duplicated editing display blocks** (`setting_selector.py:195-215`). The `number`, `float`, and `text` editing blocks produce identical HTML — they could be collapsed into a single branch checking `input_type in ("number", "float", "text")`.

3. **`_save`/`run` return semantics are confusing** — `run()` returns `{}` for "saved, no changes" and `None` for "cancelled". But `_open_provider_editor` in `commands.py:363` treats `None` as "no changes made" (wrong — it means cancelled) and conflates the two cases. The caller at line 363:
   ```python
   if changes is None:
       console.print("[dim]No changes made.[/dim]")
       return False
   ```
   This prints "No changes made" on cancel instead of "Cancelled", which is a bug.

4. **Nav item rendering leaks HTML into `SettingOption.text`** (`commands.py:500-502`). The provider list sets `text=label` where `label` contains raw `<style>` tags (e.g., `"<style fg='green'>(Active)</style>"`). This mixes display concerns — the selector's own rendering adds its own styling, so injecting HTML into the text field creates a fragile coupling.

5. **`_on_save` sentinel pattern** — The class uses `_ON_SAVE = False` as a class-level constant but `_on_save` as an instance attribute. The class constant is only used as the initial value for the instance attribute. It's unnecessary indirection — just initialize `self._on_save = False` directly.

6. **No input length limits on text fields** — `handle_char` (line 507) has no max-length guard. A user could type indefinitely into a text/number/float field.

## Minor Issues

- **`description` field on `SettingOption`** is used as a hacky side-channel in `_open_provider_editor` to stash the unmasked API key. It's not used for its intended purpose (descriptive help text shown to the user).
- **`_CURSOR = "  "`** class constant (`setting_selector.py:52`) is defined but never referenced.

---

## Summary of Changes

**Dead code to remove:**
- `_total_setting_rows()` method
- `select_setting()` function
- `_CURSOR` class constant
- `delete_model_price()` and `list_model_prices()` from `ConfigManager`

**Architecture improvements:**
- Extract per-type rendering from `_get_display_text()` into helper methods
- Collapse duplicated editing display branches
- Fix the `None` vs `{}` return value bug in `_open_provider_editor`
- Remove HTML injection from `SettingOption.text` for the provider list
- Remove `_ON_SAVE` class constant, initialize `self._on_save = False` directly
- Add max-length guard on text input buffer
