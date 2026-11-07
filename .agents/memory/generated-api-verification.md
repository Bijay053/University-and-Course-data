---
name: Generated API verification
description: Generator success, generated barrel exports and repository-wide type checks are separate checks.
---
Do not equate successful Orval generation with a passing library type check.
The split Zod generator can append an export for a schema file that it does
not emit; inspect generated barrel exports against the actual output.

**Why:** The generator completed successfully but introduced a missing-module
error through its barrel export. Independent existing helper-library errors
also caused the chained repository-wide check to fail.

**How to apply:** Check generated imports/exports after regeneration. Report
artifact type checks separately from repository-wide library checks; never
describe the latter as passing just because the generator or portal passes.
