# Question Tool Code Review

## Review Date
2025-01-06

## Overview
Review of `src/tools/select_option.py` for bugs and dead code.

---

## Dead Code

### 1. `self.title` (line 33)
**Issue:** Stored but never used anywhere in the code.

**Recommendation:** Remove this assignment.

---

### 2. `self._line_count` (lines 35, 136)
**Issue:** Calculated and stored but never read.

**Recommendation:** Remove these lines.

---

### 3. `console` parameter (line 352)
**Issue:** The `console` parameter is passed to `SelectionPanel.__init__` but ignored. A new console is always created in the `run()` method (lines 356-357).

**Recommendation:** Remove the `console` parameter from the function signature and from `SelectionPanel.__init__`.

---

### 4. `self.selections = None` (line 46)
**Issue:** Only relevant for multi-question mode, but always set to `None` in single-question mode. It's never read in single-question mode.

**Recommendation:** Remove this line (only initialize `self.selections` in multi-question mode).

---

## Bugs

### 5. Non-functional summary screen (lines 106-119, 244)
**Issue:** The `_showing_summary` flag is set to `True` at line 244, and there's code to render a summary (lines 106-119), but `event.app.exit()` is called immediately after. The summary is rendered but never displayed to the user.

**Recommendation:** Either:
- Display the summary and wait for user to press Enter to confirm, OR
- Remove the summary screen code entirely since it's not functional

---

### 6. Context missing console in agentic.py (lines 1288-1296)
**Issue:** The manually-built `context` dict doesn't include `'console'`, so even if the tool tried to use the console parameter, it would be None.

**Recommendation:** Add `'console': self._get_console()` to the context dict, OR remove the unused console parameter from select_option (see issue #3).

---

### 7. Redundant validation (lines 338-340)
**Issue:** In multi-question mode, the function validates `question` and `options` parameters that aren't required and won't be used.

**Recommendation:** Move these validation checks inside the `if questions is None:` block.

---

## Minor Issues

### 8. Inaccurate type hint (line 330)
**Issue:** The `questions` parameter type hint is incorrect. It's typed as `List[Dict[str, List[Dict[str, str]]]]` but the actual structure is `List[Dict[str, Any]]`.

**Recommendation:** Change to `Optional[List[Dict[str, Any]]]` or use a TypedDict.

---

## Summary of Changes

### Remove Dead Code:
1. Delete `self.title` assignment (line 33)
2. Delete `self._line_count` initialization and update (lines 35, 136)
3. Remove `console` parameter from `select_option()` function (line 352)
4. Remove `console` parameter from `SelectionPanel.__init__` (line 21)
5. Remove `self.selections = None` in single-question mode (line 46)

### Fix Bugs:
6. Remove or fix the summary screen feature (either display it before exit, or remove the code at lines 106-119 and 244)
7. Add `'console': self._get_console()` to the context dict in agentic.py (line 1295), OR remove unused console handling
8. Restructure validation to only check `question`/`options` when in single-question mode

### Type Hint Improvement:
9. Fix `questions` parameter type hint to `Optional[List[Dict[str, Any]]]` or use a TypedDict

---

## Priority

**High Priority:**
- Bug #5 (non-functional summary screen - misleading feature)
- Bug #7 (redundant validation - causes incorrect error messages)

**Medium Priority:**
- Dead code #1, #2, #4 (unused attributes)
- Bug #6 (context missing console - affects potential future use)

**Low Priority:**
- Dead code #3 (console parameter - cosmetic)
- Minor issue #8 (type hint - documentation only)
