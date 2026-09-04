---
name: Fee-term conversion safety
description: Invariant for preserving or converting whole-program tuition amounts.
---

A fee extracted as `Full Course` must remain `Full Course` unless an explicit conversion mode divides it by an authoritative course duration. A compatibility or “prevent rollup” flag must never change only the term to `Annual` while retaining the total amount.

**Why:** A shared recipe default relabeled UTAS whole-program totals as annual fees without changing the amounts, producing implausible annual values across repeated scrapes.

**How to apply:** Preserve source amount and term by default. If consumers require an annual equivalent, use the explicit full-course-to-annual conversion and keep regression coverage for both default and legacy recipe settings.