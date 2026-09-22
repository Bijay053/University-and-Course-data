---
name: Developer-free recovery UX
description: Product rule for blocked automatic course recovery and user-facing remediation.
---

When automatic recovery cannot recognize an individual course page, the user action is to submit the exact official course URL through the bounded course-report flow. Do not expose scraper configuration, regexes, internal repair stages, or “manual developer investigation” as the remediation.

**Why:** The product requirement is fully developer-free recovery. Official URLs are understandable user evidence, while eligibility and delivery decisions must remain independently verified by the server.

**How to apply:** Any blocked or zero-course recovery state should explain that no course was changed, open the official-course-URL form directly, and preserve all server-side eligibility and online-only safeguards. An exact endpoint-validated official course URL must survive stale catalogue discovery filters, but it must still pass global non-degree and staging guards. Show “staged” only for persisted staged rows, and keep retry available until the reported URL actually produces one.

Requirement recovery must work through the normal app for both existing records and new scrapes, not depend on one-off repair scripts. Qualification-based admission is a valid alternative to a numeric score only when backed by deterministic official-source evidence; missing IELTS bands remain unresolved.

**Why:** Overall IELTS and generic admission prose previously concealed unresolved requirements. Real extractor evidence must be tested end to end: fabricated test method names can pass while actual extraction provenance is rejected.

**How to apply:** Preserve source-bound proof through staging and repair, invalidate it when the underlying values change, and prefill the affected course and issue when requesting a supporting official URL. Never interpret missing information as “not required.”