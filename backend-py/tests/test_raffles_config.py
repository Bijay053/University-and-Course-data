from __future__ import annotations

import pytest

from app.services.scraper.config import set_uni_config
from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.extractors.study_mode import (
    has_authoritative_online_location_evidence,
)
from app.services.scraper.pipelines.single_course import extract_course


def _config():
    return load_uni_config(
        slug="raffles-university",
        scrape_url="https://raffles-university.edu.my/",
        university_id=48,
        name="Raffles University",
    )


def test_raffles_recipe_uses_bounded_english_catalogue() -> None:
    config = _config()

    assert config.discovery.seed_urls == [
        "https://raffles-university.edu.my/programme/"
    ]
    assert config.discovery.bfs_page_budget == 1
    assert config.discovery.expected_min_courses == 35
    assert config.discovery.allow_url_patterns == [
        r"^https?://raffles-university\.edu\.my/programme/[^/?#]+/?$"
    ]


def test_raffles_recipe_suppresses_test_format_noise_and_sets_campus() -> None:
    config = _config()

    assert config.extraction.study_mode.online_only_requires_strong_evidence is True
    assert config.extraction.skip_per_course_browser is True
    assert config.extraction.skip_browser_rescue is True
    assert config.extraction.default_course_location == (
        "Raffles University Medini Campus, Iskandar Puteri, Johor"
    )
    assert config.extraction.fees.default_currency == "MYR"
    assert config.extraction.fees.currency_override == "MYR"
    assert config.extraction.fees.fee_crit_min_aud == 2000


def test_title_owned_online_mode_blocks_default_campus() -> None:
    evidence = [
        {
            "field_key": "study_mode",
            "value": "Online",
            "method": "study_mode:title_keyword",
        }
    ]

    assert has_authoritative_online_location_evidence("Online", evidence)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "expected_mode", "expected_location"),
    [
        (
            "Bachelor of Business Administration (Honours)",
            None,
            "Raffles University Medini Campus, Iskandar Puteri, Johor",
        ),
        ("Master of Education - [ODL]", "Online", None),
    ],
)
async def test_raffles_delivery_and_campus_are_course_appropriate(
    title: str,
    expected_mode: str | None,
    expected_location: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.scraper.extractors import gemini_primary

    async def _skip_primary(*args, **kwargs):
        return {}, 0.0, 0, 0, {"skipped": True, "skip_reason": "offline_test"}

    monkeypatch.setattr(gemini_primary, "extract_primary", _skip_primary)
    set_uni_config(_config())
    html = f"""
    <html>
      <head><title>{title}</title></head>
      <body>
        <main>
          <h1>{title}</h1>
          <p>Duration: 2 years</p>
          <p>Intakes: January, May, September</p>
          <p>International fee: RM 25,000 per year</p>
          <section>
            <h2>International Student English Entry Requirement</h2>
            <p>IELTS: 5.5</p>
            <p>TOEFL Essentials (Online): 8</p>
          </section>
        </main>
      </body>
    </html>
    """

    result = await extract_course(
        "https://raffles-university.edu.my/programme/example/",
        country="Malaysia",
        html=html,
        use_ai_fallback=False,
    )

    assert result.get("error") is None
    assert result["payload"].get("study_mode") == expected_mode
    assert result["payload"].get("course_location") == expected_location