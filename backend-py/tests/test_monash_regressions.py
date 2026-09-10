"""Regression coverage for the Monash Funnelback discovery incident."""
from __future__ import annotations

import re

import pytest

from app.services.scraper.config.context import set_uni_config
from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.discovery_cache_scope import discovery_cache_scope_key
from app.services.scraper.extractors import study_mode
from app.services.scraper.guards import filter_non_degree_candidates


def _config(db_scrape_config: dict | None = None):
    return load_uni_config(
        slug="monash",
        name="Monash University",
        scrape_url="https://www.monash.edu",
        university_id=34,
        db_scrape_config=db_scrape_config,
    )


def test_monash_funnelback_recipe_beats_stale_admin_category_rules():
    cfg = _config({
        "admin_config": {
            "discovery": {
                "seed_urls": ["https://www.monash.edu/study/short-courses"],
                "allow_url_patterns": ["/professional-certificate-"],
                "block_url_patterns": [],
                "always_browser_discover": False,
                "expected_min_courses": 5,
                "non_degree_classifier": {
                    "force_url_patterns": [],
                    "force_title_patterns": [],
                },
            },
            "extraction": {"url_rewrites": []},
        }
    })

    assert cfg.discovery.always_browser_discover is True
    assert cfg.discovery.expected_min_courses == 400
    assert cfg.discovery.seed_urls == [
        "https://www.monash.edu/study/courses/find-a-course"
    ]
    assert cfg.discovery.allow_url_patterns == [
        r"^https://www\.monash\.edu/study/courses/find-a-course/"
        r"[a-z0-9-]+-(?!pdm[0-9]{4}/?$|pdd[0-9]{4}/?$)"
        r"[a-z][a-z0-9]*[0-9]{4}/?$"
    ]
    assert "/professional-certificate-" in (
        cfg.discovery.non_degree_classifier.force_url_patterns
    )
    assert r"\bProfessional Certificate\b" in (
        cfg.discovery.non_degree_classifier.force_title_patterns
    )
    assert cfg.extraction.url_rewrites[0].append_query == "international=true"
    assert cfg.extraction.filters.domestic_only.require_international_evidence is True
    assert cfg.extraction.filters.reject_parttime_only is True
    assert cfg.extraction.staging.require_international_fee is False
    assert cfg.extraction.staging.stage_on_parser_error is True


def test_monash_recipe_change_narrowly_invalidates_only_its_old_cache_scope():
    cfg = _config()
    stub_scope = discovery_cache_scope_key(
        scrape_url="https://www.monash.edu",
        discovery_config={"seed_urls": [], "allow_url_patterns": []},
    )
    old_two_seed_scope = discovery_cache_scope_key(
        scrape_url="https://www.monash.edu",
        discovery_config={
            "seed_urls": [
                "https://www.monash.edu/study/courses/find-a-course"
                "?f.Tabs%7Cundergraduate=Undergraduate",
                "https://www.monash.edu/study/courses/find-a-course"
                "?f.Tabs%7Cgraduate=Graduate",
            ],
            "allow_url_patterns": [
                r"^https://www\.monash\.edu/study/courses/find-a-course/"
                r"[a-z0-9-]+-[a-z][a-z0-9]*[0-9]{4}/?$"
            ],
        },
    )
    current_scope = discovery_cache_scope_key(
        scrape_url="https://www.monash.edu",
        discovery_config=cfg.discovery,
    )

    assert current_scope != stub_scope
    assert current_scope != old_two_seed_scope


def test_monash_course_code_filter_excludes_live_professional_development_urls():
    cfg = _config()
    allow = re.compile(cfg.discovery.allow_url_patterns[0], re.IGNORECASE)
    observed = {
        "https://www.monash.edu/study/courses/find-a-course/"
        "2023-international-evidence-based-guideline-for-the-assessment-and-"
        "management-of-polycystic-ovary-syndrome-pcos-pdm1176": False,
        "https://www.monash.edu/study/courses/find-a-course/"
        "adhd-in-adolescent-and-adult-psychiatry-a-clinical-approach-pdm1209": False,
        "https://www.monash.edu/study/courses/find-a-course/"
        "ai-adventures-teaching-tomorrow-in-schools-pdd1092": False,
        "https://www.monash.edu/study/courses/find-a-course/accounting-b2029": True,
        "https://www.monash.edu/study/courses/find-a-course/accounting-b6038": True,
        "https://www.monash.edu/study/courses/find-a-course/"
        "business-administration-b4001": True,
    }

    assert {url: bool(allow.search(url)) for url in observed} == observed


def test_monash_non_degree_results_are_removed_before_extraction():
    cfg = _config()
    degree = {
        "name": "Bachelor of Business - B2000",
        "url": "https://www.monash.edu/study/courses/find-a-course/business-b2000",
    }
    professional = {
        "name": "Professional Certificate of Business Administration - B9001",
        "url": (
            "https://www.monash.edu/study/courses/find-a-course/"
            "professional-certificate-of-business-administration-b9001"
        ),
    }
    graduate_certificate = {
        "name": "Graduate Certificate of Business Administration - B4001",
        "url": (
            "https://www.monash.edu/study/courses/find-a-course/"
            "business-administration-b4001"
        ),
    }

    # This phrase remains fail-open fleet-wide because another institution may
    # legitimately award a degree-bearing Professional Certificate.
    globally_kept, globally_dropped = filter_non_degree_candidates([professional])
    assert globally_kept == [professional]
    assert globally_dropped == []

    classifier = cfg.discovery.non_degree_classifier
    kept, dropped = filter_non_degree_candidates(
        [degree, professional, graduate_certificate],
        force_url_patterns=classifier.force_url_patterns,
        force_title_patterns=classifier.force_title_patterns,
    )

    assert kept == [degree, graduate_certificate]
    assert dropped == [professional | {"non_degree_reason": "forced_url_pattern"}]


async def test_labelled_online_mode_is_authoritative_over_physical_location():
    html = """
    <main>
      <h1>Master of Business</h1>
      <div class="course-summary">
        <div>Study mode</div><div>Online</div>
        <div>Location</div><div>Melbourne</div>
      </div>
    </main>
    """

    result = await study_mode.extract(
        html,
        "https://www.monash.edu/study/courses/find-a-course/business-b6000",
    )

    assert len(result) == 1
    assert result[0].value == "Online"
    assert result[0].method == "study_mode:label"
    evidence = [{
        "field_key": result[0].field_key,
        "value": result[0].value,
        "normalized": result[0].normalized,
        "method": result[0].method,
        "confidence": result[0].confidence,
    }]
    assert study_mode.has_authoritative_online_location_evidence("Online", evidence)


@pytest.mark.asyncio
async def test_monash_online_survives_synthetic_melbourne_in_full_pipeline():
    from app.services.scraper.pipelines.single_course import extract_course

    cfg = _config()
    # Reproduce the stale production/admin default which caused the incident.
    cfg.extraction.default_course_location = "Melbourne"
    set_uni_config(cfg)
    html = """
    <html><body><main>
      <h1>Master of Business (B6000)</h1>
      <dl><dt>Study mode</dt><dd>Online</dd></dl>
      <p>International students are eligible to apply.</p>
      <p>International student fee: A$48,000 per year.</p>
      <p>Duration: 2 years full-time.</p>
      <p>Intake: February and July.</p>
      <p>IELTS overall score of 6.5 with no band below 6.0.</p>
    </main></body></html>
    """

    result = await extract_course(
        "https://www.monash.edu/study/courses/find-a-course/business-b6000",
        country="Australia",
        html=html,
        use_ai_fallback=False,
    )
    payload = result["payload"]

    assert payload["study_mode"] == "Online"
    assert payload.get("course_location") in (None, "")
    assert payload["extraction_method"]["study_mode"] in {
        "study_mode:label",
        "study_mode:strong_label",
    }
    assert payload["study_load"] == "Full Time"
    assert payload["international_fee"] == 48000