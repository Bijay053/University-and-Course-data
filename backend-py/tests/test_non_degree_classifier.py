"""System-wide non-degree candidate gate regressions."""
from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.scraper.config.context import set_uni_config
from app.services.scraper.config.schema import (
    DiscoveryConfig,
    NonDegreeClassifierConfig,
    UniConfig,
)
from app.services.scraper.guards import (
    LIKELY_DEGREE,
    OBVIOUS_NON_DEGREE,
    UNKNOWN_CANDIDATE,
    classify_course_candidate,
    classify_static_course_page,
    filter_non_degree_candidates,
)


def _config(**classifier_kwargs: Any) -> UniConfig:
    return UniConfig(
        slug="classifier-test",
        name="Classifier Test University",
        base_url="https://example.edu",
        scrape_url="https://example.edu/courses",
        discovery=DiscoveryConfig(
            non_degree_classifier=NonDegreeClassifierConfig(**classifier_kwargs)
        ),
    )


@pytest.mark.parametrize(
    ("url", "title", "reason"),
    [
        (
            "https://www.leedsbeckett.ac.uk/courses/cpd-courses/business/ilm/"
            "ilm-3-building-your-foundation-in-coaching/",
            "Building Your Foundation in Coaching",
            "non_degree_url",
        ),
        (
            "https://example.edu/courses/coaching-foundations",
            "Professional Development Workshop: Coaching Foundations",
            "non_degree_title",
        ),
        (
            "https://example.edu/courses/health/standalone-module/",
            "Clinical Skills Update",
            "non_degree_url",
        ),
        (
            "https://example.edu/courses/modules",
            "Advanced Coaching Practice",
            "non_degree_url",
        ),
    ],
)
def test_obvious_non_degree_candidates_are_rejected(
    url: str,
    title: str,
    reason: str,
) -> None:
    outcome, actual_reason = classify_course_candidate(url, title)
    assert outcome == OBVIOUS_NON_DEGREE
    assert actual_reason == reason


def test_degree_route_nested_under_cpd_catalogue_is_preserved() -> None:
    outcome, reason = classify_course_candidate(
        "https://www.leedsbeckett.ac.uk/courses/cpd-courses/business/"
        "msc-executive-leadership-military-accelerated-masters-route/",
        "MSc Executive Leadership Military Accelerated Masters Route",
    )
    assert outcome == LIKELY_DEGREE
    assert reason == "degree_title"


@pytest.mark.parametrize(
    "title",
    [
        "Bachelor of Arts",
        "Master of Cyber Security",
        "Doctor of Philosophy",
        "Diploma of Business",
        "Certificate in Data Analytics",
        "ICT50220 Diploma of Information Technology",
    ],
)
def test_recognized_awards_are_preserved(title: str) -> None:
    outcome, _ = classify_course_candidate(
        f"https://example.edu/courses/{title.lower().replace(' ', '-')}",
        title,
    )
    assert outcome == LIKELY_DEGREE


def test_degree_prefix_absence_alone_fails_open() -> None:
    outcome, reason = classify_course_candidate(
        "https://example.edu/courses/project-management",
        "Project Management",
    )
    assert outcome == UNKNOWN_CANDIDATE
    assert reason == "insufficient_evidence"


def test_allow_override_protects_legitimate_cpd_catalogue() -> None:
    outcome, reason = classify_course_candidate(
        "https://example.edu/courses/cpd-courses/executive-leadership",
        "Executive Leadership",
        allow_url_patterns=[r"/cpd-courses/"],
    )
    assert outcome == LIKELY_DEGREE
    assert reason == "allowed_url_override"


def test_force_override_drops_site_specific_unknown_shape() -> None:
    outcome, reason = classify_course_candidate(
        "https://example.edu/learning/bespoke/coaching",
        "Coaching Foundations",
        force_url_patterns=[r"/learning/bespoke/"],
    )
    assert outcome == OBVIOUS_NON_DEGREE
    assert reason == "forced_url_pattern"


def test_disabled_classifier_fails_open() -> None:
    outcome, reason = classify_course_candidate(
        "https://example.edu/courses/cpd-courses/coaching",
        "CPD Course in Coaching",
        enabled=False,
    )
    assert outcome == UNKNOWN_CANDIDATE
    assert reason == "classifier_disabled"


def test_static_page_uses_explicit_course_owned_evidence() -> None:
    outcome, reason, title = classify_static_course_page(
        "https://example.edu/courses/coaching",
        """
        <html><body><main>
          <h1>Coaching Foundations</h1>
          <dl><dt>Course type</dt><dd>Short course</dd></dl>
        </main></body></html>
        """,
    )
    assert outcome == OBVIOUS_NON_DEGREE
    assert reason == "non_degree_page_evidence"
    assert title == "Coaching Foundations"


def test_degree_h1_wins_over_module_words_in_body() -> None:
    outcome, reason, _ = classify_static_course_page(
        "https://example.edu/courses/msc-leadership",
        """
        <html><body><main>
          <h1>MSc Leadership</h1>
          <p>Study individual modules covering strategy and coaching.</p>
        </main></body></html>
        """,
    )
    assert outcome == LIKELY_DEGREE
    assert reason == "degree_title"


def test_static_page_ignores_footer_and_hidden_non_degree_chrome() -> None:
    outcome, reason, _ = classify_static_course_page(
        "https://example.edu/courses/leadership",
        """
        <html><body>
          <main>
            <h1>Leadership Programme</h1>
            <div hidden>
              <dl><dt>Course type</dt><dd>Short course</dd></dl>
            </div>
            <p>Develop advanced leadership skills.</p>
          </main>
          <footer>
            <dl><dt>Course type</dt><dd>Short course</dd></dl>
          </footer>
        </body></html>
        """,
    )
    assert outcome == UNKNOWN_CANDIDATE
    assert reason == "insufficient_evidence"


@pytest.mark.parametrize("inert_tag", ["script", "style", "noscript", "template"])
def test_static_page_ignores_inert_embedded_markup(inert_tag: str) -> None:
    outcome, reason, title = classify_static_course_page(
        "https://example.edu/courses/leadership",
        f"""
        <html><body>
          <{inert_tag}>
            <h1>CPD Course in Leadership</h1>
            <dl><dt>Course type</dt><dd>Short course</dd></dl>
          </{inert_tag}>
          <main>
            <h1>Leadership Programme</h1>
            <p>Develop advanced leadership skills.</p>
          </main>
        </body></html>
        """,
    )
    assert outcome == UNKNOWN_CANDIDATE
    assert reason == "insufficient_evidence"
    assert title == "Leadership Programme"


def test_discovery_title_allow_override_persists_through_static_gate() -> None:
    outcome, reason, _ = classify_static_course_page(
        "https://example.edu/courses/cpd-courses/executive-leadership",
        """
        <html><body><main>
          <h1>Executive Leadership</h1>
          <dl><dt>Course type</dt><dd>CPD</dd></dl>
        </main></body></html>
        """,
        discovery_title="Executive Leadership Award",
        allow_title_patterns=[r"Leadership Award$"],
    )
    assert outcome == LIKELY_DEGREE
    assert reason == "allowed_title_override"


def test_filter_preserves_rich_link_dicts_and_returns_drop_reason() -> None:
    degree = {
        "url": "https://example.edu/courses/msc-data-science",
        "name": "MSc Data Science",
        "provider_payload": {"code": "MSC01"},
    }
    cpd = {
        "url": "https://example.edu/courses/cpd-courses/coaching",
        "name": "Coaching Foundations",
        "provider_payload": {"code": "CPD01"},
    }
    unknown = {
        "url": "https://example.edu/courses/project-management",
        "name": "Project Management",
    }

    kept, dropped = filter_non_degree_candidates([degree, cpd, unknown])

    assert kept == [degree, unknown]
    assert dropped[0]["provider_payload"] == {"code": "CPD01"}
    assert dropped[0]["non_degree_reason"] == "non_degree_url"


def test_schema_defaults_to_enabled_with_empty_overrides() -> None:
    cfg = DiscoveryConfig()
    assert cfg.non_degree_classifier.enabled is True
    assert cfg.non_degree_classifier.allow_url_patterns == []
    assert cfg.non_degree_classifier.force_url_patterns == []


@pytest.mark.asyncio
async def test_static_non_degree_page_returns_skip_before_browser_and_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_uni_config(_config())
    browser_calls: list[str] = []
    gemini_calls: list[str] = []

    async def _browser(*args: Any, **kwargs: Any) -> None:
        browser_calls.append("called")
        raise AssertionError("browser must not run for a classified non-degree page")

    async def _gemini(*args: Any, **kwargs: Any) -> None:
        gemini_calls.append("called")
        raise AssertionError("Gemini must not run for a classified non-degree page")

    monkeypatch.setitem(
        sys.modules,
        "app.services.scraper.per_course_browser",
        SimpleNamespace(maybe_browser_refetch=_browser),
    )
    monkeypatch.setitem(
        sys.modules,
        "app.services.scraper.extractors.gemini_primary",
        SimpleNamespace(extract_primary=_gemini),
    )

    emitted: list[dict[str, Any]] = []

    async def _emit(event: str, message: str, **kwargs: Any) -> None:
        emitted.append({"event": event, "message": message, **kwargs})

    from app.services.scraper.pipelines.single_course import extract_course

    result = await extract_course(
        "https://example.edu/courses/coaching-foundations",
        html="""
        <html><body><main>
          <h1>Coaching Foundations</h1>
          <dl><dt>Course type</dt><dd>Short course</dd></dl>
        </main></body></html>
        """,
        emit=_emit,
    )

    assert result["error"] == "skipped:non_degree_static_page"
    assert result["skip_reason"] == "non_degree_page_evidence"
    assert result["_perf"]["non_degree_browser_avoided"] is True
    assert result["_perf"]["non_degree_ai_avoided"] is True
    assert browser_calls == []
    assert gemini_calls == []
    assert any(event.get("kind") == "non_degree_static_skip" for event in emitted)