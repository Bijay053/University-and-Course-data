---
name: Fee approval race ordering
description: Why variant-backed approval must defer fee-resolution checks until after row locking
---

For staged courses with selectable fee variants, do not reject an unresolved fee from an in-memory snapshot before acquiring and refreshing the database row lock. Another reviewer may already be committing the chosen fee; a pre-lock check rejects a course that becomes valid as soon as the concurrent selection finishes.

**Why:** A real two-writer approval/selection concurrency test found this ordering bug even though ordinary sequential approval passed.

**How to apply:** When adding approval preflight validation, distinguish immutable input errors from mutable state shared with concurrent reviewers. Validate the latter against the locked, refreshed row.