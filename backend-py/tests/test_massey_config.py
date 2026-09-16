from __future__ import annotations

import re

import pytest

from app.services.scraper.central_pages import match_central_fee
from app.services.scraper.config import set_uni_config
from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.config.schema import UniConfig
from app.services.scraper.pipelines import single_course


def _config():
    return load_uni_config(
        slug="massey",
        scrape_url="https://www.massey.ac.nz/study/courses/",
        university_id=47,
        name="Massey University",
    )


def test_massey_uses_complete_qualification_catalogue() -> None:
    config = _config()

    assert config.discovery.seed_urls == [
        "https://www.massey.ac.nz/study/all-qualifications-and-degrees/"
    ]
    assert config.discovery.bfs_page_budget == 1
    assert config.discovery.expected_min_courses >= 170
    assert config.discovery.skip_sitemap_fallback is True
    assert config.discovery.skip_browser_discovery is True


def test_massey_only_promotes_qualification_detail_urls() -> None:
    config = _config()
    detail_pattern = re.compile(config.discovery.course_detail_url_patterns[0])
    candidate_pattern = re.compile(config.discovery.force_candidate_url_patterns[0])

    detail_url = (
        "https://www.massey.ac.nz/study/all-qualifications-and-degrees/"
        "bachelor-of-accountancy-UBACC/"
    )
    assert detail_pattern.fullmatch(detail_url)
    assert candidate_pattern.fullmatch(
        "/study/all-qualifications-and-degrees/bachelor-of-accountancy-UBACC/"
    )

    assert not detail_pattern.fullmatch(
        "https://www.massey.ac.nz/study/all-qualifications-and-degrees/"
    )
    assert not detail_pattern.fullmatch("https://www.massey.ac.nz/study/courses/")
    assert not detail_pattern.fullmatch(
        "https://www.massey.ac.nz/study/planning-your-study/"
        "prospectus-booklets-and-guides/"
    )


def test_massey_pins_safe_extraction_sources_and_concurrency() -> None:
    config = _config()

    assert config.extraction.max_parallel_fetch == 2
    assert config.extraction.scrape_do_static is True
    assert config.extraction.skip_per_course_browser is True
    assert config.extraction.fees.central_page == (
        "https://www.massey.ac.nz/study/fees-and-funding/"
        "tuition-fees-for-international-students/"
    )
    assert config.extraction.fees.currency_override == "NZD"
    assert config.extraction.fees.central_fee_exact_match_only is True
    assert config.extraction.fees.central_fee_priority is True
    assert config.extraction.fees.discard_domestic_fee is True
    assert "Non-tuition fees" in config.extraction.fees.reject_keywords
    assert config.extraction.english.central_page is None
    assert config.extraction.english.degree_level_defaults["undergraduate"].ielts == 6.0
    assert config.extraction.english.degree_level_defaults["postgraduate"].ielts == 6.5


def test_massey_fee_match_joins_catalogue_code_url_to_central_name() -> None:
    """Massey's detail title/code is not repeated in its central fee rows.

    The checked-in exact-only policy must still apply the row when the
    authoritative detail URL provides an exact slug join; enabling fuzzy name
    matching would be less safe for similarly named qualifications.
    """
    fees = [
        {
            "program_pattern": "Bachelor of Accountancy",
            "international_fee": 38_080,
            "currency": "NZD",
        },
        {
            "program_pattern": "Bachelor of Business",
            "international_fee": 38_080,
            "currency": "NZD",
        },
    ]

    matched, confidence = match_central_fee(
        "Bachelor of Accountancy – Bacc",
        fees,
        exact_only=True,
        course_url=(
            "https://www.massey.ac.nz/study/all-qualifications-and-degrees/"
            "bachelor-of-accountancy-UBACC/"
        ),
    )

    assert matched is not None
    assert matched["program_pattern"] == "Bachelor of Accountancy"
    assert confidence == "exact"


def test_massey_fee_match_url_code_join_does_not_fuzzy_match_wrong_slug() -> None:
    fees = [
        {
            "program_pattern": "Bachelor of Accountancy",
            "international_fee": 38_080,
            "currency": "NZD",
        },
    ]

    matched, confidence = match_central_fee(
        "Bachelor of Accountancy – Bacc",
        fees,
        exact_only=True,
        course_url=(
            "https://www.massey.ac.nz/study/all-qualifications-and-degrees/"
            "bachelor-of-business-UBBSS/"
        ),
    )

    assert matched is None
    assert confidence == "none"


@pytest.mark.asyncio
async def test_massey_detail_fee_beats_conflicting_central_priority_tuple(monkeypatch) -> None:
    """A course-owned detail tuple must stay coherent when central data disagrees."""
    set_uni_config(_config())

    async def _no_browser(*args, **kwargs):
        return {}, [], None, False

    monkeypatch.setattr(
        "app.services.scraper.per_course_browser.maybe_browser_refetch",
        _no_browser,
    )

    url = (
        "https://www.massey.ac.nz/study/all-qualifications-and-degrees/"
        "bachelor-of-accountancy-UBACC/"
    )
    html = """
    <html><body>
      <h1>Bachelor of Accountancy</h1>
      <h2>Fees and scholarships</h2>
      <h3>2026 tuition fees</h3>
      <p>International students: $38,080</p>
    </body></html>
    """
    out = await single_course.extract_course(
        url=url,
        html=html,
        country="New Zealand",
        use_ai_fallback=False,
        central_data={
            "fees": [
                {
                    "program_pattern": "Bachelor of Accountancy",
                    "international_fee": 44_444,
                    "currency": "NZD",
                    "per": "semester",
                    "fee_year": 2027,
                }
            ],
            "fee_page_url": "https://www.massey.ac.nz/central-fees",
        },
    )

    payload = out["payload"]
    assert {
        key: payload.get(key)
        for key in ("international_fee", "currency", "fee_term", "fee_year")
    } == {
        "international_fee": 38_080,
        "currency": "NZD",
        "fee_term": "Annual",
        "fee_year": 2026,
    }
    detail_evidence = [
        evidence
        for evidence in out["evidence"]
        if evidence.get("field_key") == "international_fee"
    ]
    assert detail_evidence
    assert detail_evidence[0]["method"] == "fee.massey_qualification_detail"
    assert not any(
        evidence.get("method", "").startswith("central_page:fees")
        for evidence in out["evidence"]
        if evidence.get("field_key") in {
            "international_fee",
            "currency",
            "fee_term",
            "fee_year",
        }
    )
    assert payload["extraction_method"]["international_fee"] == (
        "fee.massey_qualification_detail"
    )


@pytest.mark.asyncio
async def test_generic_central_priority_clears_stale_fee_year(monkeypatch) -> None:
    """Replacing a page fee with yearless central data must clear its year."""
    set_uni_config(
        UniConfig.model_validate(
            {
                "slug": "central-priority-test",
                "name": "Central Priority Test",
                "base_url": "https://central.example.com",
                "scrape_url": "https://central.example.com",
                "extraction": {
                    "fees": {
                        "central_fee_priority": True,
                    }
                },
            }
        )
    )

    async def _no_browser(*args, **kwargs):
        return {}, [], None, False

    monkeypatch.setattr(
        "app.services.scraper.per_course_browser.maybe_browser_refetch",
        _no_browser,
    )

    out = await single_course.extract_course(
        url="https://central.example.com/cyber",
        html="""
        <html><body>
          <h1>Bachelor of Cybersecurity</h1>
          <p>2026 international tuition fee: AUD 32,000 per year.</p>
        </body></html>
        """,
        country="Australia",
        use_ai_fallback=False,
        central_data={
            "fees": [
                {
                    "program_pattern": "Bachelor of Cybersecurity",
                    "international_fee": 24_000,
                    "currency": "AUD",
                    "per": "year",
                }
            ],
            "fee_page_url": "https://central.example.com/fees",
        },
    )

    assert out["payload"]["international_fee"] == 24_000
    assert out["payload"]["currency"] == "AUD"
    assert out["payload"]["fee_term"] == "year"
    assert out["payload"].get("fee_year") is None
