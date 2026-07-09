---
name: Stage-0 auto-rule vs manual YAML override precedence
description: CASCADE/probe_and_configure can regenerate Stage-0 extraction_rules that silently reintroduce bugs a manual per-uni YAML fix already solved.
---

CASCADE auto-recovery (triggered after a poor-quality scrape) calls
`probe_and_configure`, which uses Gemini to regenerate per-field CSS/XPath/regex
"Stage-0" rules and stores them in `universities.scrape_config.auto_config.extraction_rules`.
These rules are applied in `single_course.py` *before* the deterministic
extractors, writing directly into `payload[field]`. Because the deterministic
extractor's later write is a `payload.setdefault(...)`, a bad auto-rule wins
the race silently — no error, no visible conflict — even when a human has
already fixed the same field via a manual per-uni YAML override (e.g.
`extraction.course_name.h1_css_selector`).

**Why:** auto-rule regeneration has no awareness of manual per-uni YAML
overrides; it re-derives from whatever sample HTML it fetches and can easily
pick a different (still wrong) heading/selector than the deterministic
extractor would.

**How to apply:** when a per-uni YAML sets a manual override for a field's
extraction strategy, the Stage-0 auto-rule application must explicitly skip
that field (checked via `get_uni_config()` inside the Stage-0 block), not just
rely on write-order. If a "fixed" bug reappears with a different symptom after
a scrape failure + recovery cycle, suspect a freshly-regenerated Stage-0 rule
before re-debugging the original manual fix — check
`universities.scrape_config.auto_config.extraction_rules` and its
`_extraction_rules_generated_at` timestamp.
