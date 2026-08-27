---
name: Audience-scoped fee boundaries
description: Safety rules for extracting tuition from pages that SSR-render multiple student audiences.
---

Treat machine-readable audience containers as authoritative only when the owner identifies one exclusive audience, the fee label’s nearest audience owner is that container, and the label explicitly describes tuition, annual/year-one tuition, or full-course tuition. Mixed audience owners must fail closed, and ancillary charges must never be promoted to tuition.

**Why:** Flattened pages can mix domestic and international values, but broad or nested audience wrappers can also contain application fees, deposits, services charges, or domestic cards. Giving those containers unconditional precedence creates high-confidence false tuition.

**How to apply:** Prefer an exclusive international/non-resident container over flattened text; reject mixed domestic/home/local/resident owners, preserve non-resident as international, enforce nearest-owner boundaries, and require explicit tuition/course/year semantics before assigning a fee.