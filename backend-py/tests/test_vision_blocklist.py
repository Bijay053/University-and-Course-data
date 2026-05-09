"""Week 1 Prompt 3 — vision OCR image path block-list.

The block-list ``_GENERIC_MARKETING_PATH_BLOCKS`` in
``app.services.scraper.per_course_vision`` must filter out site-wide
marketing / chrome image directories before any image is sent to Gemini.
This test pins the additional patterns added under Week 1 Prompt 3 so a
future rename / regrouping of the constant doesn't silently drop them.
"""
from __future__ import annotations

import pytest

from app.services.scraper import per_course_vision as pcv


_NEW_BLOCKED_PATHS = (
    "/shared/cards/",
    "/shared/career-opportunities/",
    "/shared/people/",
    "/shared/video/",
    "/shared/short-content/",
    "/shared/course-hero/",
    "/shared/content-old/",
    "/shared/video-old/",
    "/laureate/shared/",
    "/cards/billy",
    "/cards/brand",
)


@pytest.mark.parametrize("blocked_path", _NEW_BLOCKED_PATHS)
def test_blocklist_pattern_present(blocked_path: str) -> None:
    """Every Prompt-3 pattern must remain in the constant."""
    assert blocked_path in pcv._GENERIC_MARKETING_PATH_BLOCKS, (
        f"Prompt 3 block-list pattern {blocked_path!r} missing from "
        f"_GENERIC_MARKETING_PATH_BLOCKS — Week 1 Prompt 3 regression."
    )


@pytest.mark.parametrize("blocked_path", _NEW_BLOCKED_PATHS)
def test_image_with_blocked_path_filtered(blocked_path: str) -> None:
    """Images whose absolute URL contains a Prompt-3 path must be dropped
    by ``_extract_img_candidates`` before reaching the OCR pipeline."""
    img_url = f"https://example.edu.au{blocked_path}sample.png"
    legit_url = "https://example.edu.au/courses/master-of-it/english.png"
    html = (
        "<html><body>"
        f'<img src="{img_url}" alt="chrome">'
        f'<img src="{legit_url}" alt="english requirements table">'
        "</body></html>"
    )
    candidates, _tier0 = pcv._extract_img_candidates(
        html, base_url="https://example.edu.au/courses/master-of-it"
    )
    candidate_urls = [u for u, _alt in candidates]
    assert img_url not in candidate_urls, (
        f"Block-listed URL {img_url!r} leaked into OCR candidates: "
        f"{candidate_urls}"
    )
    # The legitimate image must still be returned so the test confirms
    # the filter is selective, not a blanket drop.
    assert legit_url in candidate_urls, (
        f"Legit course image {legit_url!r} unexpectedly filtered while "
        f"asserting block-list — got candidates: {candidate_urls}"
    )


def test_mixed_case_url_still_blocked() -> None:
    """Block-list comparison runs against ``absolute.lower()`` so a
    mixed-case CDN URL (e.g. ``/Shared/Cards/`` from a CMS that preserves
    the Title-Case directory name) must still be filtered."""
    img_url = "https://example.edu.au/SHARED/Cards/Hero.PNG"
    html = f'<html><body><img src="{img_url}" alt="x"></body></html>'
    candidates, _ = pcv._extract_img_candidates(
        html, base_url="https://example.edu.au/courses/x"
    )
    assert img_url not in [u for u, _ in candidates], (
        "Mixed-case block-listed URL leaked through case-sensitive check"
    )


def test_existing_chrome_paths_still_blocked() -> None:
    """Sanity check: pre-existing block-list entries (Week-0 baseline)
    must still be present so this prompt didn't accidentally narrow the
    list."""
    for legacy in (
        "/how-to-apply",
        "/shared/marketing",
        "/shared/footer",
        "/marketing/",
    ):
        assert legacy in pcv._GENERIC_MARKETING_PATH_BLOCKS
