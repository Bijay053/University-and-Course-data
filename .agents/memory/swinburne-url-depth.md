---
name: Swinburne AEM extraction throughput
description: Why Swinburne needs bounded CPU concurrency and site-specific safe HTML compaction despite static course pages.
---

Swinburne course pages are static but unusually large AEM documents. Keep remote AI, browser rescue, and OCR disabled; also bound same-process extraction concurrency so repeated DOM parsing does not make tail tasks cross the shared wall-clock deadline. Safely collapse known navigation trees while preserving text and critical-value fingerprints.

**Why:** Twelve concurrent 300–400 KB pages saturated one Celery child with repeated BeautifulSoup parsing. Tail tasks timed out at 90 seconds even though network requests returned quickly. Eight concurrent pages plus additional parity-checked navigation compaction improved observed throughput from about 12 to 16 courses/minute and removed timeouts during the verification window.

**How to apply:** Treat this as a CPU-bound static-site profile, not a network-bound one. Do not raise concurrency reflexively. Recheck extraction parity and live timeout counts after any AEM template change.

Swinburne SSR pages include Domestic and International values in the same HTML. Duration, campus, and yearly tuition must be read from the International audience nodes in the hero and fee panels rather than from flattened page text. An International yearly fee of `$0.00` is an unpublished-fee placeholder, not tuition; leave the fee blank and suppress fallback to adjacent Domestic yearly or total amounts.

**Why:** Flattened extraction selected Domestic `$17,399`/`$73,080`, pathway durations, and the key-dates heading “Last date to apply” even though the visible International panels showed `$44,840`/`$43,920`, 3/2 years, and Hawthorn.

**How to apply:** Keep audience boundaries structural. Prefer the International child; when its duration says only “Full-time”, use the shared hero duration. If no campus hero exists, leave location blank rather than running generic page-wide location fallbacks.