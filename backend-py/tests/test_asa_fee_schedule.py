"""Regression coverage for ASA's official international fee schedule."""

from __future__ import annotations

import pytest

from app.services.scraper.config.loader import load_uni_config


@pytest.mark.parametrize(
    ("slug", "university_id"),
    [("asa", 2), ("asahe", None), ("asahe", 9)],
)
def test_asa_pins_official_international_fee_schedule(
    slug: str,
    university_id: int | None,
) -> None:
    cfg = load_uni_config(
        slug=slug,
        name="ASA Institute of Higher Education",
        scrape_url="https://asahe.edu.au",
        university_id=university_id,
    )

    assert cfg.extraction.fees.fees_pdf_url == (
        "https://cdn.prod.website-files.com/68660d9286e56f070b7bebe7/"
        "696f704ddf123bd8c8d982b0_2026%20Fees%20Schedule%20-%20"
        "International%20Student.pdf"
    )
    assert cfg.extraction.fees.pdf_parser == "columnar"
    assert cfg.extraction.fees.prefer_annual_over_total is True
    assert cfg.extraction.fees.pdf_overrides_page_regex is True