---
name: Evidence proof persistence
description: Verify source-authority proof through actual persistence, not only extractor output.
---
Source-authority decisions must be tested after the real evidence persistence boundary, not against an unpersisted extractor fixture.

**Why:** Display snippets can be truncated during persistence. A complete in-memory structured record can therefore pass mocked selection tests while every real multi-option course fails source verification.

**How to apply:** Keep complete proof separate from display excerpts and test a realistically large record through persistence and reload. Legacy truncated excerpts alone cannot prove the omitted alternatives; require matching complete evidence or fresh official-source verification.

Round-trip tests must preserve the university configuration context at both extraction and staging boundaries.

**Why:** Resetting the context after extraction made an HSU persistence test reject an unpublished fee under generic staging policy, rather than exercise the intended university-specific review behavior.

**How to apply:** Re-enter the same recipe context for staging without bypassing its gate. Exercise replacement under a new scrape job; same-job duplicate rejection is a different contract.

Reusing identical evidence is safer than superseding a candidate and appending
an identical selected copy.

**Why:** Duplicate suppression at the database boundary can retain the earlier
superseded copy and discard the selected copy, losing the citation despite a
correct in-memory winner.

**How to apply:** Reuse the original proof when reaffirming course authority;
assert selected citations after persistence, including overall-only requirements.