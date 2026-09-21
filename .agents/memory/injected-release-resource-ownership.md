---
name: Injected release resource ownership
description: Ownership rule for hermetic release-script seams and cleanup.
---

Test-injected release resources are borrowed unless the harness explicitly transfers ownership; cleanup may remove only resources the release script created itself.

**Why:** A hermetic cleanup test initially passed while deleting the injected reconciler source because production normally supplies that helper as a temporary file.

**How to apply:** Whenever a release test seam injects paths, commands, or files into production orchestration, track ownership separately from presence and exercise the EXIT trap before accepting the seam.