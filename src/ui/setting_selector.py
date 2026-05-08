"""Reusable component for interactive setting selection and editing.

Toolbar-hosted via ``ToolbarInteraction`` — renders compact config/
provider selectors in the bottom toolbar area instead of an inline
prompt_toolkit ``Application``.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Callable, Union

from ui.toolbar_interactions import (
    ToolbarInteraction,
    run_toolbar_interaction,
    escape_html,
    styled,
    make_section,
)


@dataclass
class SettingOption:
    """A single setting option with validation support."""
    key: str                          # Config key
    text: str                         # Display label
    value: Any                        # Current value
    options: List[Dict[str, Any]] = None  # For enum-style: {"value": x, "text": y}
    input_type: str = "select"        # "select", "text", "number", "boolean", "float", "options"
    description: str = ""
    min_val: Union[int, float] = None
    max_val: Union[int, float] = None
    step: Union[int, float] = None
    validate_fn: Callable[[Any], bool] = None  # Custom validator
    on_text: str = ""   # Custom label when value is truthy (e.g. "Active")
    off_text: str = ""  # Custom label when value is falsy (e.g. "-")

    def __post_init__(self):
        if self.options is None:
            self.options = []


@dataclass
class SettingCategory:
    """A category containing related settings."""
    title: str
    icon: str = ""
    settings: List[SettingOption] = field(default_factory=list)


class SettingSelector(ToolbarInteraction):
    """Interactive setting selector with live value editing — toolbar-hosted.

    Boolean settings display with ON / OFF labels.
    Enter toggles booleans directly. A Save option sits at the bottom.
    Non-boolean types support inline editing in the toolbar.
    """

    _CURSOR = "> "
    _ON_SAVE = False  # Sentinel: cursor is on the Save button

    def __init__(
        self,
        categories: List[SettingCategory],
        title: str = "Settings",
        on_change: Callable[[str, str, Any], None] = None,  # Called on value change
        show_save: bool = True,  # Whether to show the Save button
        chat_manager: Any = None,  # For swarm interrupt support
    ):
        """Initialize the setting selector.

        Args:
            categories: List of SettingCategory objects with settings
            title: Panel title
            on_change: Callback(key, action, value) when setting changes
            show_save: Whether to display the Save button
            chat_manager: Optional ChatManager for swarm admin interrupt polling
        """
        super().__init__()
        self.categories = categories
        self.title = title
        self.on_change = on_change
        self.show_save = show_save
        self._chat_manager = chat_manager

        self.current_cat_idx = 0
        self.current_setting_idx = 0
        self._on_save = self._ON_SAVE
        self.editing_value = False
        self.input_buffer = ""
        self._initial_values: Dict[str, Any] = {
            s.key: s.value
            for cat in categories
            for s in cat.settings
        }

    def _get_current_setting(self) -> Optional[SettingOption]:
        """Get the currently selected setting."""
        if 0 <= self.current_cat_idx < len(self.categories):
            cat = self.categories[self.current_cat_idx]
            if 0 <= self.current_setting_idx < len(cat.settings):
                return cat.settings[self.current_setting_idx]
        return None

    def _format_value(self, setting: SettingOption) -> str:
        """Format a setting value for display."""
        if setting.input_type in ("boolean", "nav"):
            if setting.on_text and setting.value:
                return setting.on_text
            if setting.off_text and not setting.value:
                return setting.off_text
            return "ON" if setting.value else "OFF"
        elif setting.input_type == "select" and setting.options:
            for opt in setting.options:
                if opt.get("value") == setting.value:
                    return opt.get("text", str(setting.value))
        elif isinstance(setting.value, bool):
            return "Yes" if setting.value else "No"
        elif isinstance(setting.value, float) and setting.step and setting.step < 1:
            return f"{setting.value:.2f}"
        return str(setting.value)

    def _total_setting_rows(self) -> int:
        """Total navigable rows across all categories."""
        return sum(len(cat.settings) for cat in self.categories)

    def _is_boolean_setting(self, setting: Optional[SettingOption]) -> bool:
        """Check if a setting is a boolean toggle or nav item."""
        return setting is not None and setting.input_type in ("boolean", "nav")

    def _get_flat_index(self) -> int:
        """Get the flat index of the currently focused setting."""
        idx = 0
        for c in range(self.current_cat_idx):
            idx += len(self.categories[c].settings)
        idx += self.current_setting_idx
        return idx

    def _flat_to_position(self, flat_idx: int) -> tuple[int, int]:
        """Convert a flat index to (category_idx, setting_idx)."""
        for c_idx, cat in enumerate(self.categories):
            if flat_idx < len(cat.settings):
                return c_idx, flat_idx
            flat_idx -= len(cat.settings)
        # Past all settings — return last
        last_cat = len(self.categories) - 1
        return last_cat, len(self.categories[last_cat].settings) - 1


    # ------------------------------------------------------------------
    # ToolbarInteraction interface
    # ------------------------------------------------------------------

    def render(self) -> str:
        """Return compact HTML for bottom-toolbar rendering.

        Shows title/category header, a windowed list around the focused
        setting, focused value/edit buffer, and a short controls hint.
        Options-type settings expand to show a windowed radio list.
        """
        lines = []

        # Header line: title + category + controls hint
        title_text = escape_html(self.title) if self.title else ""
        show_headers = len(self.categories) > 1
        cat = self.categories[self.current_cat_idx]
        cat_text = f" \u2014 {escape_html(cat.title)}" if show_headers else ""

        hint = self._controls_hint()
        header = f"<b>{title_text}{cat_text}</b>    <style fg='#555555'>{hint}</style>"
        lines.append(header)

        # Settings window
        setting = self._get_current_setting()
        is_options_focused = (
            not self._on_save
            and setting is not None
            and setting.input_type == "options"
            and setting.options
        )

        if is_options_focused:
            lines.extend(self._render_options_window(setting))
        else:
            lines.extend(self._render_settings_window())

        # Save button
        if self.show_save:
            if self._on_save:
                lines.append(f"{self._CURSOR}<b>[ Save ]</b>")
            else:
                lines.append("  [ Save ]")

        return make_section(lines=lines)

    def _controls_hint(self) -> str:
        """Build the short controls hint based on current state."""
        setting = self._get_current_setting()
        if self._on_save:
            return "\u21b5 save  Esc save &amp; close"
        if self.editing_value:
            if setting and setting.input_type in ("number", "float", "text"):
                return "Type  \u21b5 confirm  Esc discard"
            if setting and setting.input_type == "select":
                return "\u2191\u2193 change  \u21b5 confirm  Esc discard"
            return "\u21b5 confirm  Esc discard"
        # Navigation mode
        if setting and setting.input_type == "nav":
            return "\u2191\u2193 navigate  \u21b5 open  Esc cancel"
        if setting and setting.input_type == "options":
            return "\u2191\u2193 change option  \u21b5 select  Esc cancel"
        if setting and self._is_boolean_setting(setting):
            return "\u2191\u2193 navigate  \u21b5 toggle  Esc cancel"
        if setting:
            return "\u2191\u2193 navigate  \u21b5 edit  Esc cancel"
        return "\u2191\u2193 navigate  Esc cancel"

    def _render_settings_window(self) -> list:
        """Render a windowed list of settings around the focused one."""
        lines_out = []
        _MAX_VISIBLE = 8
        total = self._total_setting_rows()

        if total == 0:
            return lines_out

        flat_idx = self._get_flat_index() if not self._on_save else total

        if total <= _MAX_VISIBLE:
            visible_start = 0
            visible_end = total
        else:
            half = _MAX_VISIBLE // 2
            visible_start = max(0, flat_idx - half)
            visible_end = min(total, visible_start + _MAX_VISIBLE)
            if visible_end - visible_start < _MAX_VISIBLE:
                visible_start = max(0, visible_end - _MAX_VISIBLE)

        if visible_start > 0:
            lines_out.append(
                f'<style fg="#888888">  \u22ef {visible_start} more above \u22ef</style>'
            )

        for idx in range(visible_start, visible_end):
            cat_idx, setting_idx = self._flat_to_position(idx)
            setting = self.categories[cat_idx].settings[setting_idx]
            is_focused = (
                cat_idx == self.current_cat_idx
                and setting_idx == self.current_setting_idx
                and not self._on_save
            )
            is_editing = is_focused and self.editing_value
            lines_out.append(self._render_setting_line(setting, is_focused, is_editing))

        if visible_end < total:
            lines_out.append(
                f'<style fg="#888888">  \u22ef {total - visible_end} more below \u22ef</style>'
            )

        return lines_out

    def _render_options_window(self, setting: "SettingOption") -> list:
        """Render a windowed radio-button list of options for an options-type setting."""
        lines_out = []
        _MAX_OPTIONS = 7  # 7 options + 1 header line = 8 total, matching settings window
        options = setting.options or []
        total = len(options)

        if total == 0:
            return lines_out

        # Show the setting label
        lines_out.append(f"<b>{escape_html(setting.text)}</b>")

        # Find currently selected option index
        current_idx = next(
            (i for i, o in enumerate(options) if o.get("value") == setting.value), 0
        )

        if total <= _MAX_OPTIONS:
            visible_start = 0
            visible_end = total
        else:
            half = _MAX_OPTIONS // 2
            visible_start = max(0, current_idx - half)
            visible_end = min(total, visible_start + _MAX_OPTIONS)
            if visible_end - visible_start < _MAX_OPTIONS:
                visible_start = max(0, visible_end - _MAX_OPTIONS)

        if visible_start > 0:
            lines_out.append(
                f'<style fg="#888888">    \u22ef {visible_start} more above \u22ef</style>'
            )

        for o_idx in range(visible_start, visible_end):
            opt = options[o_idx]
            opt_text = escape_html(opt.get("text", str(opt.get("value", ""))))
            opt_value = opt.get("value")
            is_current = opt_value == setting.value
            desc = opt.get("description", "")

            if is_current:
                prefix = "  > "
                marker = "\u25c9"
                color = "#5F9EA0"
                lines_out.append(
                    f'{prefix}<style fg="{color}">{marker}</style> '
                    f'<style fg="{color}" bold="true">{opt_text}</style>'
                    + (f'  <style fg="gray">{escape_html(desc)}</style>' if desc else "")
                )
            else:
                prefix = "    "
                marker = "\u25cb"
                desc_suffix = f'  <style fg="gray">{escape_html(desc)}</style>' if desc else ""
                lines_out.append(
                    f'{prefix}<style fg="gray">{marker}</style> '
                    f'{opt_text}{desc_suffix}'
                )

        if visible_end < total:
            lines_out.append(
                f'<style fg="#888888">    \u22ef {total - visible_end} more below \u22ef</style>'
            )

        return lines_out

    def _render_setting_line(
        self, setting: "SettingOption", is_focused: bool, is_editing: bool
    ) -> str:
        """Render a single setting as one toolbar line."""
        prefix = self._CURSOR if is_focused else "  "
        label = escape_html(setting.text)

        if setting.input_type == "boolean":
            is_on = bool(setting.value)
            tag = "ON" if is_on else "OFF"
            if is_focused:
                color = "green" if is_on else "red"
                return f'{prefix}<style fg="{color}" bold="true">{tag}</style>  <b>{label}</b>'
            return f'{prefix}<style fg="gray">{tag}</style>  {label}'

        if setting.input_type == "nav":
            tag = escape_html(self._format_value(setting))
            if is_focused:
                return f'{prefix}<b>{label}</b>  <style fg="#5F9EA0">{tag}</style>'
            return f'{prefix}{label}  <style fg="gray">{tag}</style>'

        if is_editing:
            if setting.input_type in ("number", "float", "text"):
                buf = escape_html(self.input_buffer)
                cursor = styled("\u258c", fg="#FFFFFF")
                return f'{prefix}<b>{label}:</b>  <style fg="yellow">{buf}{cursor}</style>'
            if setting.input_type == "select" and setting.options:
                tag = escape_html(self._format_value(setting))
                return f'{prefix}<style fg="yellow" bold="true">{tag}</style>  <b>{label}</b>'

        if setting.input_type == "select" and setting.options:
            tag = escape_html(self._format_value(setting))
            if is_focused:
                return f'{prefix}<style fg="#5F9EA0" bold="true">{tag}</style>  <b>{label}</b>'
            return f'{prefix}<style fg="gray">{tag}</style>  {label}'

        # Generic: label: value
        val = escape_html(self._format_value(setting))
        if is_focused:
            return f'{prefix}<b>{label}:</b>  <style fg="#5F9EA0">{val}</style>'
        return f'{prefix}{label}:  <style fg="gray">{val}</style>'

    # ------------------------------------------------------------------
    # Key handling (ToolbarInteraction interface)
    # ------------------------------------------------------------------

    def handle_key(self, event: object) -> bool:
        """Handle a key event forwarded from the prompt_toolkit application."""
        name = self._extract_key_name(event)

        # ── Editing mode ──
        if self.editing_value:
            return self._handle_editing_key(event, name)

        # ── Navigation mode ──
        if name == "up":
            self._handle_up()
            return True
        if name == "down":
            self._handle_down()
            return True
        if name == "enter":
            self._handle_enter()
            return True
        if name == "escape":
            if self._on_save:
                self._save_and_finish()
            else:
                self.cancel()
            return True
        if name == "right":
            self._enter_edit_mode()
            return True
        if name in ("backspace", "delete"):
            return True  # no-op in navigation mode

        return True  # consume all keys while active

    def _handle_editing_key(self, event: object, name: str) -> bool:
        """Handle keys while editing a text/number/float/select setting."""
        setting = self._get_current_setting()

        if name == "escape":
            self.editing_value = False
            self.input_buffer = ""
            return True

        if name == "enter":
            if setting and setting.input_type in ("text", "number", "float"):
                if self._validate_input(setting, self.input_buffer):
                    if setting.input_type == "number":
                        new_val = int(self.input_buffer)
                    elif setting.input_type == "float":
                        new_val = float(self.input_buffer)
                    else:
                        new_val = self.input_buffer
                    self._apply_change(setting.key, new_val)
                self.editing_value = False
                self.input_buffer = ""
            elif setting and setting.input_type == "select" and setting.options:
                # Confirm current selection in edit mode
                self.editing_value = False
            return True

        if name == "up":
            if setting and setting.input_type == "select" and setting.options:
                current_idx = next(
                    (i for i, o in enumerate(setting.options) if o.get("value") == setting.value), 0
                )
                new_idx = max(0, current_idx - 1)
                self._apply_change(setting.key, setting.options[new_idx].get("value"))
            return True

        if name == "down":
            if setting and setting.input_type == "select" and setting.options:
                current_idx = next(
                    (i for i, o in enumerate(setting.options) if o.get("value") == setting.value), 0
                )
                new_idx = min(len(setting.options) - 1, current_idx + 1)
                self._apply_change(setting.key, setting.options[new_idx].get("value"))
            return True

        if name == "backspace":
            if self.input_buffer:
                self.input_buffer = self.input_buffer[:-1]
            return True

        if name == "delete":
            if self.input_buffer:
                self.input_buffer = self.input_buffer[:-1]
            return True

        # Printable characters for text/number/float editing
        if setting and setting.input_type in ("text", "number", "float"):
            data = self._extract_key_data(event)
            if data and len(data) == 1 and ord(data) >= 32:
                if setting.input_type == "number":
                    if data.isdigit() or (data == '-' and not self.input_buffer):
                        self.input_buffer += data
                elif setting.input_type == "float":
                    if (data.isdigit()
                            or (data == '.' and '.' not in self.input_buffer)
                            or (data == '-' and not self.input_buffer)):
                        self.input_buffer += data
                else:
                    self.input_buffer += data
            return True

        return True

    def _handle_up(self):
        """Move selection up (or change option for options type)."""
        if self.editing_value:
            return
        setting = self._get_current_setting()
        if setting and setting.input_type == "options" and setting.options:
            current_idx = next(
                (i for i, o in enumerate(setting.options) if o.get("value") == setting.value), 0
            )
            new_idx = max(0, current_idx - 1)
            self._apply_change(setting.key, setting.options[new_idx].get("value"))
            return
        self._navigate_up()

    def _handle_down(self):
        """Move selection down (or change option for options type)."""
        if self.editing_value:
            return
        setting = self._get_current_setting()
        if setting and setting.input_type == "options" and setting.options:
            current_idx = next(
                (i for i, o in enumerate(setting.options) if o.get("value") == setting.value), 0
            )
            new_idx = min(len(setting.options) - 1, current_idx + 1)
            self._apply_change(setting.key, setting.options[new_idx].get("value"))
            return
        self._navigate_down()

    def _handle_enter(self):
        """Handle Enter in navigation mode."""
        if self._on_save:
            self._save_and_finish()
            return

        setting = self._get_current_setting()
        if not setting:
            return

        # Nav: signal drill-down
        if setting.input_type == "nav":
            self.finish({"_nav": setting.key})
            return

        # Boolean: toggle directly
        if self._is_boolean_setting(setting):
            self._apply_change(setting.key, not setting.value)
            return

        # Select: cycle to next option
        if setting.input_type == "select" and setting.options:
            current_idx = next(
                (i for i, o in enumerate(setting.options) if o.get("value") == setting.value), 0
            )
            new_idx = (current_idx + 1) % len(setting.options)
            self._apply_change(setting.key, setting.options[new_idx].get("value"))
            return

        # Options: confirm and save
        if setting.input_type == "options" and setting.options:
            self._save_and_finish()
            return

        # Text/number/float: enter edit mode
        if setting.input_type in ("text", "number", "float"):
            self.editing_value = True
            self.input_buffer = str(setting.value)
            return

    def _enter_edit_mode(self):
        """Enter edit mode for the current setting (if applicable)."""
        if self.editing_value or self._on_save:
            return
        setting = self._get_current_setting()
        if setting and setting.input_type not in ("boolean", "nav"):
            self.editing_value = True
            if setting.input_type in ("text", "number", "float"):
                self.input_buffer = str(setting.value)

    def _save_and_finish(self):
        """Compute changes and finish the interaction."""
        changes = {}
        for cat in self.categories:
            for setting in cat.settings:
                if setting.value != self._initial_values.get(setting.key):
                    changes[setting.key] = setting.value
        self.finish(changes if changes else {})

    # ------------------------------------------------------------------
    # Key extraction helpers (duck-type prompt_toolkit events)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_key_name(event: object) -> str:
        """Extract a normalized key name from a prompt_toolkit KeyPressEvent."""
        try:
            seq = getattr(event, "key_sequence", None)
            if seq:
                press = seq[-1]
                key = getattr(press, "key", None)
                name = getattr(key, "name", None) if key is not None else None
                if not name:
                    name = getattr(press, "data", None)
                return SettingSelector._normalize_key_name(name) if name else ""
        except Exception:
            pass
        return ""

    @staticmethod
    def _extract_key_data(event: object) -> str:
        """Extract printable character data from a prompt_toolkit KeyPressEvent."""
        try:
            seq = getattr(event, "key_sequence", None)
            if seq:
                press = seq[-1]
                data = getattr(press, "data", None)
                return data if data else ""
        except Exception:
            pass
        return ""

    @staticmethod
    def _normalize_key_name(name: str) -> str:
        """Normalize key names with platform-dependent variants."""
        name = name.lower()
        if name in ("c-m", "controlm", "\r", "\n"):
            return "enter"
        if name in ("c-h", "controlh"):
            return "backspace"
        if name in ("c-i", "controli", "\t"):
            return "tab"
        if name in ("space",):
            return " "
        if name in ("delete", "c-d"):
            return "delete"
        return name

    # ------------------------------------------------------------------
    # Runner
    # ------------------------------------------------------------------

    def run(self, chat_manager: Any = None) -> Optional[Dict[str, Any]]:
        """Display the setting selector via toolbar interaction.

        Runs in a minimal prompt_toolkit Application via
        ``run_toolbar_interaction``.  Nothing is written to the chat
        transcript.

        Args:
            chat_manager: Optional ChatManager for swarm interrupt support.
                          Overrides the one set in __init__.

        Returns:
            Dict of {key: new_value} for changed settings, {} if saved
            with no changes, or None if cancelled / interrupted.
        """
        cm = chat_manager or self._chat_manager

        # Pre-check: if swarm work is pending, skip interaction entirely.
        if cm is not None and hasattr(cm, "has_pending_swarm_work"):
            if cm.has_pending_swarm_work():
                return None

        result = run_toolbar_interaction(self, chat_manager=cm)

        if result is None:
            return None

        if self.was_cancelled():
            return None

        return self.result()

    def _validate_input(self, setting: SettingOption, value: str) -> bool:
        """Validate user input for a setting."""
        if setting.validate_fn:
            try:
                if setting.input_type == "number":
                    return setting.validate_fn(int(value))
                elif setting.input_type == "float":
                    return setting.validate_fn(float(value))
                return setting.validate_fn(value)
            except (ValueError, TypeError):
                return False

        # Built-in validation
        if setting.input_type == "number":
            try:
                int_val = int(value)
                if setting.min_val is not None and int_val < setting.min_val:
                    return False
                if setting.max_val is not None and int_val > setting.max_val:
                    return False
                if setting.step is not None and setting.step > 0:
                    if (int_val - setting.min_val if setting.min_val is not None else int_val) % setting.step != 0:
                        return False
                return True
            except ValueError:
                return False
        elif setting.input_type == "float":
            try:
                float_val = float(value)
                if setting.min_val is not None and float_val < setting.min_val:
                    return False
                if setting.max_val is not None and float_val > setting.max_val:
                    return False
                if setting.step is not None and setting.step > 0:
                    base = setting.min_val if setting.min_val is not None else 0.0
                    remainder = abs(float_val - base) % setting.step
                    if remainder > 1e-9 and abs(remainder - setting.step) > 1e-9:
                        return False
                return True
            except ValueError:
                return False

        return len(value) > 0

    def _apply_change(self, key: str, new_value: Any) -> None:
        """Apply a setting change."""
        for cat in self.categories:
            for setting in cat.settings:
                if setting.key == key:
                    old_value = setting.value
                    setting.value = new_value
                    if self.on_change and old_value != new_value:
                        self.on_change(key, "change", new_value)
                    return

    def _navigate_down(self):
        """Move selection down one row, wrapping into the Save button."""
        if self._on_save:
            return
        cat = self.categories[self.current_cat_idx]
        if self.current_setting_idx < len(cat.settings) - 1:
            self.current_setting_idx += 1
        elif self.current_cat_idx < len(self.categories) - 1:
            self.current_cat_idx += 1
            self.current_setting_idx = 0
        elif self.show_save:
            self._on_save = True

    def _navigate_up(self):
        """Move selection up one row, off the Save button if needed."""
        if self._on_save:
            self._on_save = False
            return
        if self.current_setting_idx > 0:
            self.current_setting_idx -= 1
        elif self.current_cat_idx > 0:
            self.current_cat_idx -= 1
            self.current_setting_idx = len(self.categories[self.current_cat_idx].settings) - 1







