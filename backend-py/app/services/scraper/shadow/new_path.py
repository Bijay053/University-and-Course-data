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

Initially (Phase 1 — harness validation): no transformation at all. The new
path returns a deep copy of the old result. The diff will always be clean, which
proves the harness is wired correctly before any ACAP-specific changes are made.

Week 2 step 2 (ACAP domestic_only migration): apply the YAML-driven domestic_only
filter to the already-extracted payload. The diff will show whether the new filter
produces the same staging decisions as the old shared-code filter.

Week 2 step 3 (re NameError fix in shared code): applied directly to shared code;
shadow mode is not needed for a pure import fix with no behaviour change.

Adding a new uni transformation:
  1. Set SHADOW_MODE_UNI_IDS=<uni_id> in the environment.
  2. Add a block under ``# Per-uni transformations`` below, keyed by uni_id.
  3. Run shadow mode for 5 fresh scrapes (each ≥1 hour apart against live site).
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
        Transformed copy of old_result. Initially identical (no-op).
    """
    new_result = copy.deepcopy(old_result)

    # ── Per-uni transformations ────────────────────────────────────────────────
    # Add uni-specific logic here as each uni migrates.
    # Each block should only transform new_result["payload"] — leave url, name,
    # evidence, and error alone unless the transformation explicitly changes them.
    #
    # Example for ACAP domestic_only YAML migration (Week 2 step 2):
    #
    # if uni_id == 41:  # ACAP
    #     from app.services.scraper.config.context import require_uni_config
    #     uc = require_uni_config()
    #     if uc.domestic_only and uc.domestic_only.text_must_appear_in:
    #         payload = new_result.get("payload") or {}
    #         page_section = payload.get("_domestic_only_check_section", "")
    #         required_section = uc.domestic_only.text_must_appear_in
    #         if page_section and page_section != required_section:
    #             new_result["payload"]["domestic_only"] = True
    # ──────────────────────────────────────────────────────────────────────────

    return new_result
