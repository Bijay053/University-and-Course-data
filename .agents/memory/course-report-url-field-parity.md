---
name: Course-report URL field parity
description: Why verified programme URLs supplied through different report fields must enter the same targeted recovery path.
---

A missing-course report's single official programme URL may arrive as `catalogue_url` rather than in the explicit course URL list. If page-owned eligibility proof establishes that it is a programme detail page, treat it as an exact targeted recovery URL; do not send it through broad catalogue discovery.

**Why:** A foundation page passed official-host and page-evidence validation, but its proof and URL were omitted from the worker target because only the explicit list received targeted semantics. The worker completed a normal catalogue scrape and never attempted the reported page.

**How to apply:** Keep arbitrary catalogue roots on the discovery path. Promote a URL from another report field only after the same official-host, connected-peer, detail-path, and strong page-owned programme checks required for explicit foundation/pathway URLs.