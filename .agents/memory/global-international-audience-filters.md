---
name: Global international-audience filters
description: Product policy for domestic-only and online-only course staging.
---

Confirmed domestic-only courses must be excluded from the international review queue.
The user changed the delivery policy on 2026-11-05: online-only and part-time-only
courses must be retained in pending review with eligibility warnings, not silently
excluded. They must not be automatically published.

**Why:** The user explicitly selected “Keep them flagged for review” when resolving
the updated scenario document against the earlier exclusion policy.

**How to apply:** Preserve delivery evidence and require eligibility review at
staging and re-extraction. Do not confuse delivery restrictions with explicit
domestic-only admission. Keep publication gated.

Retained review records need idempotent re-checks: finding the same restriction
again must not create another change audit or report new progress.

**Why:** A terminal rejected status previously prevented repeat writes. Keeping
the row pending removes that implicit protection.

**How to apply:** Check the already-recorded restriction and unchanged review
state before writing again. Preserve explicit domestic-only exclusion precedence
when both domestic-only and online-only evidence are present.

Evidence cleanup must not erase a conservative delivery restriction before the
final publication-readiness decision.

**Why:** Removing an unsupported delivery value also removed the input used to
flag the row for eligibility review, allowing later scoring to miss the hold.

**How to apply:** Keep the restriction signal separate from publishable field
values and reapply the review hold after scoring and campus splitting. Do not
weaken source-proof requirements to retain the signal.

Review status alone is not an automatic-publication guard.

**Why:** The publishing pass can rescore review rows. Courses formerly rejected
at staging became reachable by that pass when the delivery policy changed.

**How to apply:** Enforce delivery holds in both publication scoring and the
automatic approval boundary, including holds preserved only in warnings. Campus
splits must retain the hold on the original row as well as every child.

UTAS embeds a shared “may not be available” / “see the list of distance courses” advisory on ordinary course pages. Inline links can split the phrase in raw HTML and re-form it only after tag stripping, so cleanup must also run on normalized visible text. Explicit statements inside the International section remain authoritative.

**Why:** Raw-HTML-only cleanup missed the split advisory and falsely rejected mainstream courses, while genuinely unavailable courses still published explicit International-section exclusions.

**How to apply:** Ignore only the bounded shared advisory on UTAS. Preserve explicit course-level “not available to international students” evidence. Genuine online-only delivery now requires a warning, not a staging rejection.