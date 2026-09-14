from __future__ import annotations

import re

import pytest

from app.services.scraper.config import set_uni_config
from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.pipelines.single_course import extract_course


def _config():
    return load_uni_config(
        slug="mila",
        scrape_url="https://mila.edu.my/",
        university_id=49,
        name="MILA University",
    )


def test_mila_recipe_is_bounded_to_english_programme_details() -> None:
    config = _config()

    assert config.discovery.seed_urls == ["https://mila.edu.my/programmes/"]
    assert config.discovery.bfs_page_budget == 1
    assert config.discovery.skip_sitemap_fallback is True
    assert config.discovery.expected_min_courses == 35
    pattern = re.compile(config.discovery.allow_url_patterns[0])

    assert pattern.fullmatch(
        "https://mila.edu.my/programme/bachelor-of-computer-science-hons/"
    )
    assert pattern.fullmatch(
        "https://mila.edu.my/programme/master-of-business-administration-odl/"
    )
    assert not pattern.fullmatch("https://mila.edu.my/programmes/")
    assert not pattern.fullmatch(
        "https://mila.edu.my/zh/programme/%e5%95%86%e4%b8%9a%e7%ae%a1%e7%90%86/"
    )
    assert not pattern.fullmatch(
        "https://mila.edu.my/programme/%e8%ae%a1%e7%ae%97%e6%9c%ba/"
    )
    assert not pattern.fullmatch("https://mila.edu.my/school-of-education/")
    assert not pattern.fullmatch(
        "https://mila.edu.my/programme/bachelor-of-computer-science-hons/?utm=nav"
    )

    assert config.discovery.course_detail_url_patterns == [
        config.discovery.allow_url_patterns[0]
    ]
    assert config.extraction.skip_browser_rescue is True
    assert config.extraction.skip_per_course_browser is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "title", "description", "expected_mode", "expected_location"),
    [
        (
            "https://mila.edu.my/programme/bachelor-of-computer-science-hons/",
            "Bachelor of Computer Science (Hons)",
            "A campus programme with practical industry learning.",
            None,
            "MILA University, Nilai, Negeri Sembilan, Malaysia",
        ),
        (
            "https://mila.edu.my/programme/master-of-business-administration-odl/",
            "Master of Business Administration (ODL)",
            "A flexible business administration programme for working professionals.",
            "Online",
            None,
        ),
    ],
)
async def test_mila_conventional_and_odl_delivery(
    url: str,
    title: str,
    description: str,
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
      <head><title>{title} - MILA University</title></head>
      <body>
        <main>
          <h1>{title}</h1>
          <p>{description}</p>
          <p>Duration: 3 years</p>
          <p>Tuition Fees: RM23,520</p>
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
        url,
        country="Malaysia",
        html=html,
        use_ai_fallback=False,
    )

    assert result.get("error") is None
    assert result["payload"].get("study_mode") == expected_mode
    assert result["payload"].get("course_location") == expected_location