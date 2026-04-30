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

Shadow mode migration sequence for ACAP (uni_id 41)
----------------------------------------------------
Phase 1 (harness validation — DONE, 5-run streak achieved 2026-04-30):
  No transform. Deep copy only. Proved the diff machinery, report writer,
  streak counter, and contextvar pipeline are all wired correctly.

Phase 2 (domestic_only YAML validation — CURRENT):
  Transform: read DomesticOnlyFilter.enabled from the current_uni_config
  contextvar (loaded from scraper_config/unis/acap.yaml). Since acap.yaml
  has enabled=true, this is a no-op for ACAP and the shadow stays clean.
  If acap.yaml is ever accidentally set to enabled=false, the next run
  surfaces 14+ staging_disagreements — one per domestic-only course.
  Target: 5 consecutive clean runs → proceed to shared-code cutover.

Phase 3 (shared-code cutover — pending):
  Modify single_course.py to gate _is_domestic_only_page() on
  uc.extraction.filters.domestic_only.enabled. Run shadow for 5 runs.
  On clean streak: set SHADOW_CUTOVER_UNI_IDS=41, clear SHADOW_MODE_UNI_IDS.

Phase 4 onwards (subsequent ACAP migrations, each their own streak):
  - trust_vision_ocr wiring (separate PR)
  - strip_patterns wiring (separate PR)
  - re NameError fix (shared-code fix, separate regression sweep)

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

    if uni_id == 41:  # ACAP — Phase 2: domestic_only YAML config validation
        _apply_acap_domestic_only(new_result)

    # ──────────────────────────────────────────────────────────────────────────
    return new_result


def _apply_acap_domestic_only(new_result: dict[str, Any]) -> None:
    """Phase 2 transform: validate YAML domestic_only config matches old-path detection.

    Reads ``extraction.filters.domestic_only.enabled`` from the current_uni_config
    contextvar (populated from scraper_config/unis/acap.yaml for ACAP scrapes).

    Behaviour:
        enabled=True  (acap.yaml current value):
            Preserve any ``domestic_only: True`` the old path set in the payload.
            No change to new_result — this is a no-op. Shadow stays clean.

        enabled=False (misconfiguration guard):
            Clear ``domestic_only: True`` from the payload. Any course the old
            path rejected as domestic-only will surface as a staging_disagreement
            in the diff (old.staged=False vs new.staged=True). Immediately visible
            in the shadow report — catches accidental config regressions.

    Future extension (Phase 3+):
        When text_must_appear_in is added to DomesticOnlyFilter and the
        _domestic_only_check_section field is stored in the extraction payload,
        this transform can re-evaluate the domestic_only decision from the YAML
        text pattern rather than trusting the shared-code flag. Until then,
        enabled=True is a sufficient config-correctness gate.
    """
    try:
        from app.services.scraper.config.context import get_uni_config

        uc = get_uni_config()
        if uc is None:
            log.warning("shadow[acap/41] domestic_only transform: uni config not in contextvar")
            return

        enabled: bool = uc.extraction.filters.domestic_only.enabled

        if not enabled:
            # YAML says domestic_only detection is OFF for ACAP — clear any flag
            # the old shared-code path set. This will produce staging_disagreements
            # in the diff for every domestic-only course the old path rejected.
            payload: dict[str, Any] = new_result.get("payload") or {}
            if payload.get("domestic_only"):
                new_result["payload"] = {
                    k: v for k, v in payload.items() if k != "domestic_only"
                }
                log.debug(
                    "shadow[acap/41] domestic_only cleared for %s (enabled=False in YAML)",
                    new_result.get("url", "?"),
                )
        # enabled=True: no-op — deepcopy already preserved the flag

    except Exception as _exc:
        log.warning("shadow[acap/41] domestic_only transform failed: %s", _exc)
