---
name: Swinburne AEM extraction throughput
description: Why Swinburne needs bounded CPU concurrency and site-specific safe HTML compaction despite static course pages.
---

Swinburne course pages are static but unusually large AEM documents. Keep remote AI, browser rescue, and OCR disabled; also bound same-process extraction concurrency so repeated DOM parsing does not make tail tasks cross the shared wall-clock deadline. Safely collapse known navigation trees while preserving text and critical-value fingerprints.

**Why:** Twelve concurrent 300–400 KB pages saturated one Celery child with repeated BeautifulSoup parsing. Tail tasks timed out at 90 seconds even though network requests returned quickly. Eight concurrent pages plus additional parity-checked navigation compaction improved observed throughput from about 12 to 16 courses/minute and removed timeouts during the verification window.

**How to apply:** Treat this as a CPU-bound static-site profile, not a network-bound one. Do not raise concurrency reflexively. Recheck extraction parity and live timeout counts after any AEM template change.