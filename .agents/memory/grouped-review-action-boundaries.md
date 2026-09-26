---
name: Grouped review action boundaries
description: Keep high-impact grouped actions independent of the bulk-selection state
---

A grouped course row's Delete or Force Approve action must target exactly that group's members. Do not populate or reuse the review table's existing bulk-selection state to implement a per-row action.

**Why:** Reviewers can have unrelated rows selected before clicking a grouped action; merging the two sets can remove or publish courses they did not choose in that action, while the confirmation appears to refer only to the clicked group.

**How to apply:** Pass explicit IDs from the initiating action through confirmation to the final request, display the targeted rows, and test with an unrelated row already selected.