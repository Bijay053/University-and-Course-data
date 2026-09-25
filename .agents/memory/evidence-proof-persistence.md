---
name: Evidence proof persistence
description: Verify source-authority proof through actual persistence, not only extractor output.
---
Source-authority decisions must be tested after the real evidence persistence boundary, not against an unpersisted extractor fixture.

**Why:** Display snippets can be truncated during persistence. A complete in-memory structured record can therefore pass mocked selection tests while every real multi-option course fails source verification.

**How to apply:** Keep complete proof separate from display excerpts and test a realistically large record through persistence and reload. Legacy truncated excerpts alone cannot prove the omitted alternatives; require matching complete evidence or fresh official-source verification.