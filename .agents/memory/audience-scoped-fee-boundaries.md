---
name: Audience-scoped fee boundaries
description: Safety rules for extracting tuition from pages that SSR-render multiple student audiences.
---

Treat machine-readable audience containers as authoritative only when the owner identifies one exclusive audience, the fee label’s nearest audience owner is that container, and the label explicitly describes tuition, annual/year-one tuition, or full-course tuition. Mixed audience owners must fail closed, and ancillary charges must never be promoted to tuition. When the same numeric amount is repeated elsewhere without an audience label, retain the ownership from its explicit occurrence unless that value also has explicit international ownership.

**Why:** Flattened pages can mix domestic and international values, but broad or nested audience wrappers can also contain application fees, deposits, services charges, or domestic cards. Some sites also repeat a domestic card’s amount in a later generic tuition section, outside the local label window. Giving those containers or unlabeled repeats unconditional precedence creates high-confidence false tuition.

**How to apply:** Prefer an exclusive international/non-resident container over flattened text; reject mixed domestic/home/local/resident owners, preserve non-resident as international, enforce nearest-owner boundaries, carry explicit ownership across duplicate amounts, and require explicit tuition/course/year semantics before assigning a fee.