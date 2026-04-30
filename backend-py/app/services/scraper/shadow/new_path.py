"""New extraction code path for shadow-mode comparison.

This module is the injection point for per-uni migration work.

Initial state (Week 2 step 1): ``extract_new_path`` is functionally identical
to the old path. Running shadow mode against this produces a byte-identical
diff, which proves the harness is wired correctly before we make any changes.

Week 2 step 2 (ACAP domestic_only migration): override the UniConfig
contextvar here so ACAP scrapes use the YAML-driven domestic_only filter
instead of the shared if-block. Shadow mode will then run 5 fresh scrapes
and confirm the outputs are identical before cutover.

Week 2 step 3 (re NameError fix): applied to shared code after the
regression sweep over all 23 baselined unis passes.

To add a new uni to the new path:
  1. Set ``SHADOW_MODE_UNI_IDS=<uni_id>`` in the environment.
  2. Modify ``extract_new_path`` to apply the new UniConfig for that uni.
  3. Run shadow mode for 5 fresh scrapes.
  4. When the streak reaches 5, set ``SHADOW_CUTOVER_UNI_IDS=<uni_id>``
     and remove it from ``SHADOW_MODE_UNI_IDS``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.services.scraper.pipelines.single_course import extract_course

if TYPE_CHECKING:
    from app.services.scraper.per_course_vision import VisionImageCache

log = logging.getLogger(__name__)


async def extract_new_path(
    link: dict[str, Any],
    *,
    country: str | None,
    uni_pdf_data: dict | None = None,
    emit: Any = None,
    vision_image_cache: "VisionImageCache | None" = None,
    central_data: dict | None = None,
    uni_id: int | None = None,
) -> dict[str, Any]:
    """Run the new (migration target) extraction code path for one course.

    Initially identical to _extract_only() in orchestrator.py.
    Per-uni behavioural changes are added here as each uni migrates.

    Args:
        link:              {"url": ..., "name": ...} dict from discovery
        country:           University country string
        uni_pdf_data:      Pre-fetched PDF payload
        emit:              Async emit hook (for AI fallback log lines)
        vision_image_cache: Per-run vision cache for sibling coalescing
        central_data:      Pre-fetched central-pages payload
        uni_id:            University ID (used to select per-uni overrides)

    Returns:
        Same shape as _extract_only(): {"url", "name", "payload", "evidence", ...}
    """
    name = (link.get("name") or "").strip() or "Unknown course"
    url = link["url"]

    try:
        out = await extract_course(
            url,
            country=country,
            uni_pdf_data=uni_pdf_data,
            emit=emit,
            vision_image_cache=vision_image_cache,
            central_data=central_data,
        )
        out["name"] = name
        return out
    except Exception as exc:
        log.warning("extract_new_path failed for %s: %s", url, exc)
        return {
            "name": name,
            "url": url,
            "error": f"new_path_exception:{type(exc).__name__}",
            "payload": {},
            "evidence": [],
        }
