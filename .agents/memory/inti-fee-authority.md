---
name: INTI fee currency and period authority
description: Durable interpretation and correction rules for INTI programme fees.
---

INTI’s published RM amounts are total programme fees. Preserve the source amount, force the final fee currency to MYR, and label the period Full Course; never divide the amount by duration.

**Why:** AI extraction can label the same total as Annual, and a missing stored currency makes the review UI fall back to an A$ symbol. A default currency is only a fallback and is not strong enough to correct late extractor output.

**How to apply:** Use an explicit final currency override for universities whose fee currency is authoritative. Use source-value-only fee handling with a forced Full Course term for INTI. Repair old divided rows only when stored evidence identifies the original total.