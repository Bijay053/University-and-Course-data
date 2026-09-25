---
name: Audience-scoped fee boundaries
description: Safety rules for extracting tuition from pages that SSR-render multiple student audiences.
---

Treat machine-readable audience containers as authoritative only when the owner identifies one exclusive audience, the fee label’s nearest audience owner is that container, and the label explicitly describes tuition, annual/year-one tuition, or full-course tuition. Mixed audience owners must fail closed, and ancillary charges must never be promoted to tuition. When the same numeric amount is repeated elsewhere without an audience label, retain the ownership from its explicit occurrence unless that value also has explicit international ownership. If a site exposes audience only through tab controls or styling rather than generic audience attributes, use a host-scoped DOM reader for the International panel; an explicit but blank International tab must block fallback to the active domestic panel.

**Why:** Flattened pages can mix domestic and international values, but broad or nested audience wrappers can also contain application fees, deposits, services charges, or domestic cards. Some sites also repeat a domestic card’s amount in a later generic tuition section, outside the local label window. Giving those containers or unlabeled repeats unconditional precedence creates high-confidence false tuition. Tabbed SSR pages often place the active domestic amount first, so flattening or treating a blank International panel as “no signal” silently promotes the wrong audience.

**How to apply:** Prefer an exclusive international/non-resident container over flattened text; reject mixed domestic/home/local/resident owners, preserve non-resident as international, enforce nearest-owner boundaries, carry explicit ownership across duplicate amounts, and require explicit tuition/course/year semantics before assigning a fee. For tab-only sites, identify the International panel structurally and return a no-fee sentinel when that panel is present but unpublished.

Published campus alternatives are recovered fee information, not a missing fee and not permission to choose an arbitrary scalar. Preserve gross tuition, cohort dates, campus and study-route identity together; keep bursary alternatives separate.

**Why:** ULaw's flattened tabs both discarded real international fees and promoted a later-year domestic MBA amount. Most international offerings have different London/non-London prices, so a scalar-only success criterion misrepresented correctly recovered data.

**How to apply:** Validate persisted alternatives against their selected source and fee companions, render the range and individual options, and count verified recovery in Smart Fix. Reviewer approval may automatically resolve verified campus groups; ambiguity must still block publication. Refresh affected staged rows through the normal API; releasing extractor code alone does not repair historical records.

Published campus price labels do not prove that a course is offered at every labelled campus.

**Why:** ULaw London-only LLM pages include an outside-London price in a shared schedule. Treating every price as an offering both blocks valid London courses and risks inventing campuses from institution defaults.

**How to apply:** Establish actual course-owned delivery locations for the same route/cohort before narrowing applicable prices. Allow one uniform campus group when that proof excludes the other prices; never narrow from a university-default location alone.

Preserve numeric continuity across inline markup before parsing tuition and academic years, without flattening audience or table boundaries.

**Why:** ULaw splits visible amounts and years across adjacent spans. Inserting spaces made £16,500 look like £1 and broke year matching, even though the visible page was correct.

**How to apply:** Include split-number markup in financial extraction tests and verify the final persisted amount against the complete published token, not just the first regex match.