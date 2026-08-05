---
name: payload.get(key, default) None trap
description: dict.get(key, default) returns the stored None (not the default) when the key exists with value None — must use `(payload.get(key) or "").strip()` pattern
---

## The rule
Never write `payload.get("some_field", "").strip()` when the key can exist in the dict with value `None`.

`dict.get(key, default)` only uses the default when the key is **absent**. When the key is present and its value is `None`, `.get()` returns `None` — then `.strip()` crashes with `AttributeError: 'NoneType' object has no attribute 'strip'`.

**Why:** Pre-seeders (like the CSU static extractor) always write every field into the payload dict, using `None` for optional fields that weren't found. After the pre-seeder runs, the dict contains `{"course_location": None, ...}`. Any downstream consumer that guards with `payload.get("course_location", "")` then gets `None` back, not `""`.

**How to apply:**
- Anywhere you call `.strip()` on a dict value, use: `(payload.get(key) or "").strip()`  
- For boolean presence checks: `bool(payload.get(key) or "")`
- Pattern was the root cause of 101 NoneType.strip crashes in the CSU scrape — courses that had `[CSU ✓]` pre-seed entries and even a `[FIELD TRACE]` log line still failed at staging because two study-mode guard conditions at lines 5882 and 5925 of `single_course.py` used the unsafe `.get(key, "")` form.
