---
name: Developer-free recovery UX
description: Product rule for blocked automatic course recovery and user-facing remediation.
---

When automatic recovery cannot recognize an individual course page, the user action is to submit the exact official course URL through the bounded course-report flow. Do not expose scraper configuration, regexes, internal repair stages, or “manual developer investigation” as the remediation.

**Why:** The product requirement is fully developer-free recovery. Official URLs are understandable user evidence, while eligibility and delivery decisions must remain independently verified by the server.

**How to apply:** Any blocked or zero-course recovery state should explain that no course was changed, open the official-course-URL form directly, and preserve all server-side eligibility and online-only safeguards. An exact endpoint-validated official course URL must survive stale catalogue discovery filters, but it must still pass global non-degree and staging guards. Show “staged” only for persisted staged rows, and keep retry available until the reported URL actually produces one.