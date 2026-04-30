"""New extraction code path for shadow-mode comparison.

IMPORTANT — this module must NOT re-fetch course URLs.

The shadow comparison needs both paths to operate on the same already-fetched
page content. If the new path re-runs extract_course(), it triggers a second
Playwright browser session and possibly a second Gemini API call. Those are
non-deterministic: even for the same URL, a second JS-render or a second LLM
call can return different values, causing spurious diffs that have nothing to
do with the code path change being tested.

Correct pattern (Option A — single-process, single fetch):
  1. Old path fetches + extracts → produces result dict (payload, evidence, url)
  2. New path receives the old result and applies any config-driven transformation
  3. diff_staged_runs(old_results, new_results) compares the two

Shadow mode migration history for ACAP (uni_id 41)
----------------------------------------------------
Phase 1 (harness validation — COMPLETE, 5-run streak achieved 2026-04-23):
  No transform. Deep copy only. Proved the diff machinery, report writer,
  streak counter, and contextvar pipeline are all wired correctly.

Phase 2 (domestic_only YAML validation — COMPLETE, streak achieved 2026-04-30):
  Transform: read DomesticOnlyFilter.enabled from the current_uni_config
  contextvar (loaded from scraper_config/unis/acap.yaml). Since acap.yaml
  has enabled=true, this was a no-op for ACAP and the shadow stayed clean.
  _apply_acap_domestic_only() retired after streak completion (Phase 4 PR).

Phase 3 (shared-code gate — COMPLETE, 5-run streak achieved 2026-04-30):
  Gate _is_domestic_only_page() on _domestic_only_filter_enabled() in
  single_course.py. Regression sweep: 22/22 unis clean, 0 unexpected diffs.
  Shadow streak runs 15-19 confirmed behaviour-neutral under gated code.

Phase 4 (re NameError fix — IN PROGRESS):
  Fix bare `re.search`/`re.I` → `_re.search`/`_re.I` at line 2642.
  Validation: regression sweep with --expected-slugs acap (expect 0→~13
  staged). No shadow streak required — deep-copy comparator cannot detect
  the fix; regression sweep + spot-check of staged courses is the gate.

Adding a new uni transformation:
  1. Set SHADOW_MODE_UNI_IDS=<uni_id> in the environment.
  2. Add a block under ``# Per-uni transformations`` below, keyed by uni_id.
  3. Run shadow mode for 5 fresh scrapes.
  4. When clean_streak reaches 5, set SHADOW_CUTOVER_UNI_IDS=<uni_id>
     and remove it from SHADOW_MODE_UNI_IDS.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

log = logging.getLogger(__name__)


async def extract_new_path(
    old_result: dict[str, Any],
    *,
    uni_id: int | None = None,
) -> dict[str, Any]:
    """Apply new code path transformations to an already-extracted course result.

    Both old and new paths operate on the same fetched content (the old result),
    eliminating all network and LLM non-determinism from the shadow comparison.

    Args:
        old_result: result dict from the old extraction path (_extract_only).
                    Shape: {"url": ..., "name": ..., "payload": {...}, "evidence": [...]}
        uni_id:     University ID — used to select per-uni transformations below.

    Returns:
        Transformed copy of old_result.
    """
    new_result = copy.deepcopy(old_result)

    # ── Per-uni transformations ────────────────────────────────────────────────
    # Add per-uni blocks here as new migrations are validated.
    # Example:
    #   if uni_id == 42:   # ACU — Phase N: <description>
    #       _apply_acu_<transform>(new_result)
    # ──────────────────────────────────────────────────────────────────────────

    return new_result
