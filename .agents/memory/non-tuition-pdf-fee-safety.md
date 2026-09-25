---
name: Non-tuition PDF fee safety
description: Classification boundary between tuition schedules and university PDFs containing ancillary charges.
---

Documents explicitly dedicated to incidental, ancillary, non-tuition, SSAF, application, deposit, materials, equipment, or immigration/visa charges must never feed the international tuition field, even when they contain many currency amounts and course or unit names.

**Why:** Generic PDF scoring can rank any fee-labelled document as a tuition schedule, and max-amount or name-matching fallbacks can then stamp an ancillary charge onto unrelated courses as international tuition.

**How to apply:** Reject explicit non-tuition documents during link discovery, PDF classification, and fee parsing, including cached and manually configured sources. Keep genuine international tuition schedules eligible even when they include a secondary incidental-cost section.

A source rejection alone does not correct historical staged values.

**Why:** An AI explanation can misclassify the purpose of a published amount. Only the actual selected source establishes whether that amount is tuition; rejecting it does not replace values already staged.

**How to apply:** Verify the selected source, not the AI explanation. Re-extract using the ordinary isolated verification pipeline; missing replacement tuition is still a review failure, not successful repair. A no-change config loop can test a deployed deterministic correction only with recorded critical failures and fresh affected-course evidence; rejected proposals cannot use that route.