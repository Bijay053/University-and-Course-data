---
name: Course-report URL field parity
description: Why verified programme URLs supplied through different report fields must enter the same targeted recovery path.
---

A missing-course report's single official programme URL may arrive as `catalogue_url` rather than in the explicit course URL list. If page-owned eligibility proof establishes that it is a programme detail page, treat it as an exact targeted recovery URL; do not send it through broad catalogue discovery.

**Why:** A foundation page passed official-host and page-evidence validation, but its proof and URL were omitted from the worker target because only the explicit list received targeted semantics. The worker completed a normal catalogue scrape and never attempted the reported page.

The verified programme proof must survive every verification-metadata checkpoint. Merge the parent-bound request policy into durable metadata before adding measured progress; replacing it makes staging fall back to the generic category-page guard.

Self-service retries should select only directly reported URLs that have not produced staged rows across the report history. Do not block retry because unrelated catalogue courses were staged, and do not rerun a broad catalogue when an exact unresolved URL is available.

**Why:** A retry correctly targeted the Foundation page, but an early metadata write discarded its verified programme proof. The global degree-qualifier guard then rejected the page. An earlier retry gate also treated unrelated staged catalogue rows as proof that the reported page had succeeded.

**How to apply:** Keep arbitrary catalogue roots on the discovery path. Promote a URL from another report field only after the same official-host, connected-peer, detail-path, and strong page-owned programme checks required for explicit foundation/pathway URLs. Preserve that proof through all bounded-run metadata updates, and determine retry eligibility by canonical URL outcome rather than aggregate staged count.