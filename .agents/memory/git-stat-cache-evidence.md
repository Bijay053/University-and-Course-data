---
name: Git stat-cache evidence
description: Atomic byte-identical restores can cause false dirty-file reports from metadata-only Git diff modes.
---

Require content-based evidence, not just Git stat-cache differences, when
validating a byte-identical atomic restoration.

**Why:** Atomic replacement can invalidate cached file metadata without changing
the actual content. A metadata difference is not proof that an operator changed
the file, and must not justify discarding their work.

**How to apply:** Keep release and recovery gates strict about actual content
changes, while distinguishing them from metadata-only differences.
