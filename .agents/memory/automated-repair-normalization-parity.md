---
name: Automated repair normalization parity
description: Safety contract for generated extraction rules that correct populated-but-invalid values.
---

Quality detection, non-mutating replay validation, and runtime assignment must
use the same canonicalization and invalid-value grammar. A generated selector
that misses an existing valid baseline is a regression, not preservation.

**Why:** A rule can look safe during replay yet stage contaminated data if the
runtime path bypasses the normal field cleaner, or if SQL eligibility and replay
validation disagree about what counts as invalid.

**How to apply:** Route generated values through the canonical field cleaner
before both comparison and assignment. Treat missing output for a valid
baseline as a failed preservation check, and regression-test compact formatting
variants as well as the original contaminated form.