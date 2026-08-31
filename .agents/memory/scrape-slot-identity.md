---
name: Scrape slot persistence identity
description: Identity rule for multi-slot scraper cards that survive navigation and dynamic add/remove operations.
---

Use an immutable slot ID for session persistence and React card identity. Keep
the current array index separate and use it only for user-facing numbering.

**Why:** When persistence keys were based on the card's current grid position,
removing or adding cards changed those positions. A new card could then read a
neighboring slot's saved job ID and display a duplicate running scrape.

**How to apply:** Add operations must choose an unused stable ID; remove
operations must clear storage by that same ID. Never mutate an ID generator
inside a React state updater, and never derive persistence keys from a mapped
array index.