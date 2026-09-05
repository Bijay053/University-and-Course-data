"""Global enforcement tests for domestic-only and online-only courses."""
from __future__ import annotations

from app.services.scraper.config.context import current_uni_config
from app.services.scraper.config.schema import UniConfig
from app.services.scraper.guards import should_stage_course
from app.services.scraper.pipelines.single_course import (
    _domestic_only_filter_enabled,
    _duration_labeled_values,
    _infer_study_load_from_text,
    _is_domestic_only_page,
    _is_parttime_only_page,
    _parttime_only_filter_enabled,
)


def test_disabled_legacy_overrides_cannot_bypass_global_delivery_filters() -> None:
    """A stale YAML/admin `enabled: false` must not admit ineligible courses."""
    config = UniConfig.model_validate(
        {
            "slug": "legacy-disabled-example",
            "name": "Legacy Disabled Example University",
            "base_url": "https://example.edu",
            "scrape_url": "https://example.edu/courses",
            "extraction": {
                "filters": {
                    "domestic_only": {"enabled": False},
                    "online_only": {"enabled": False},
                }
            }
        }
    )
    token = current_uni_config.set(config)
    try:
        assert _domestic_only_filter_enabled() is True

        online_ok, online_reason = should_stage_course(
            "Online Master of Business",
            {
                "course_name": "Online Master of Business",
                "international_fee": 30000,
                "study_mode": "Online",
            },
            source_url="https://example.edu/courses/master-of-business-online",
        )
        assert online_ok is False
        assert online_reason == "online_only"

        domestic_ok, domestic_reason = should_stage_course(
            "Master of Business",
            {
                "course_name": "Master of Business",
                "international_fee": 30000,
                "study_mode": "Blended",
                "domestic_only": True,
            },
            source_url="https://example.edu/courses/master-of-business",
        )
        assert domestic_ok is False
        assert domestic_reason == "domestic_only"
    finally:
        current_uni_config.reset(token)


def test_scu_hidden_audience_selector_marks_domestic_only_course() -> None:
    html = """
    <form class="course-filter">
      <div style="display:none">
        <label for="course-location">Show me course information for</label>
        <select id="course-location">
          <option value="default">Domestic</option>
          <option value="international">International</option>
        </select>
      </div>
    </form>
    <div data-course="international">
      <h3>International snapshot</h3>
      <p>5 years full-time</p>
    </div>
    """
    assert _is_domestic_only_page(
        html,
        "https://www.scu.edu.au/study/courses/domestic-only/2027/",
    )


def test_scu_visible_audience_selector_keeps_international_course() -> None:
    html = """
    <form class="course-filter">
      <div>
        <label for="course-location">Show me course information for</label>
        <select id="course-location">
          <option value="default">Domestic</option>
          <option value="international">International</option>
        </select>
      </div>
    </form>
    """
    assert not _is_domestic_only_page(
        html,
        "https://www.scu.edu.au/study/courses/international/2027/",
    )


def test_adelaide_dormant_domestic_modal_does_not_reject_international_degree() -> None:
    html = """
    <meta property="studentType" content="Domestic|International"/>
    <dialog data-modal-opener="dom-modal-exclusive" role="dialog">
      <h2>This degree is only available to Australian students</h2>
    </dialog>
    <section class="degree-details">
      <p>Published fees are for international students starting in 2026.</p>
      <p>International tuition fee: $43,400</p>
      <p>CRICOS 097508M</p>
    </section>
    """
    assert not _is_domestic_only_page(
        html,
        "https://adelaide.edu.au/study/degrees/bachelor-of-arts/",
    )


def test_adelaide_domestic_student_type_metadata_rejects_course() -> None:
    html = """
    <meta property="studentType" content="Domestic"/>
    <dialog data-modal-opener="dom-modal-exclusive" role="dialog">
      <h2>This degree is only available to Australian students</h2>
    </dialog>
    <main>
      <p>Indicative annual fee: $25,000</p>
    </main>
    """
    assert _is_domestic_only_page(
        html,
        "https://adelaide.edu.au/study/degrees/"
        "graduate-certificate-in-oral-health-science/",
    )


def test_adelaide_student_type_metadata_is_attribute_order_independent() -> None:
    html = '<meta content="Domestic" data-source="degree" property="studentType"/>'
    assert _is_domestic_only_page(
        html,
        "https://www.adelaide.edu.au/study/degrees/domestic-program/",
    )


def test_adelaide_real_course_level_domestic_statement_still_rejects() -> None:
    html = """
    <dialog data-modal-opener="dom-modal-exclusive" role="dialog">
      <h2>This degree is only available to Australian students</h2>
    </dialog>
    <main>
      <p>This degree is only available to domestic applicants.</p>
    </main>
    """
    assert _is_domestic_only_page(
        html,
        "https://adelaide.edu.au/study/degrees/domestic-program/",
    )


def test_utas_soft_international_caveat_is_not_course_level_rejection() -> None:
    """Rendered/compacted UTAS HTML may retain the caveat but omit hidden tabs."""
    html = """
    <main>
      <h1>Bachelor of Information and Communication Technology</h1>
      <p>This course may not be available to international students.</p>
      <p>Study on campus in Hobart or Launceston.</p>
    </main>
    """
    assert not _is_domestic_only_page(
        html,
        "https://www.utas.edu.au/courses/tsbe/courses/p3t-bachelor-of-ict",
    )


def test_utas_hard_domestic_only_statement_still_rejects() -> None:
    html = """
    <main>
      <h1>Domestic Program</h1>
      <p>This course is only available to domestic students.</p>
    </main>
    """
    assert _is_domestic_only_page(
        html,
        "https://www.utas.edu.au/courses/example/courses/domestic-program",
    )


def test_utas_shared_distance_disclaimer_does_not_reject_on_campus_course() -> None:
    html = """
    <main>
      <p>This course may not be available to international students.</p>
      <p>Please <a href="/distance">see the list of distance courses</a>
      for available options.</p>
      <p>Study on campus in Hobart or Launceston.</p>
    </main>
    """
    assert not _is_domestic_only_page(
        html,
        "https://www.utas.edu.au/courses/example/courses/distance-program",
    )


def test_utas_advisory_only_international_panel_rejects_domestic_course() -> None:
    html = """
    <main>
      <h1>Bachelor of Outdoor and Environmental Education</h1>
      <div id="tabDomestic">
        <p>Commonwealth Supported places available</p>
      </div>
      <div id="tabInternational" hidden>
        This course may not be available to international students.
        Please see the <a href="/study/online">list of distance courses</a>
        (i.e. online and taken outside Australia) that are offered to
        international students
      </div>
    </main>
    """
    assert _is_domestic_only_page(
        html,
        "https://www.utas.edu.au/courses/arts-soc/courses/"
        "a3f-bachelor-of-outdoor-and-environmental-education?year=2026",
    )


def test_utas_substantive_international_panel_keeps_eligible_course() -> None:
    html = """
    <main>
      <p>This course may not be available to international students.</p>
      <p>Please see the list of distance courses that are offered to
      international students.</p>
      <div id="tabInternational" hidden>
        <h2>Key Information</h2>
        <p>CRICOS: 002346B</p>
        <p>Duration: Minimum 3 years</p>
        <p>Location: Hobart — Semester 1, Semester 2</p>
      </div>
    </main>
    """
    assert not _is_domestic_only_page(
        html,
        "https://www.utas.edu.au/courses/tsbe/courses/"
        "b3a-bachelor-of-business?year=2026",
    )


def test_utas_explicit_full_time_option_beats_shared_part_time_prose() -> None:
    html = """
    <section>
      <div>Duration</div>
      <div>
        Minimum 3 years, up to a maximum of 7 years.
        This course is available to study as both part-time or full-time.
      </div>
      <div>Duration</div>
      <div>
        Duration refers to the minimum and maximum amounts of time in which
        this course can be completed. Some programs are only available part time.
      </div>
    </section>
    """
    assert _is_parttime_only_page(html) is False


def test_utas_shared_part_time_prose_alone_is_not_course_level_evidence() -> None:
    html = """
    <section>
      <div>Duration</div>
      <div>
        Minimum 3 years, up to a maximum of 7 years.
        Duration refers to the minimum and maximum amounts of time in which
        this course can be completed. It will be affected by whether you choose
        to study full or part time, noting that some programs are only available
        part time.
      </div>
    </section>
    """
    assert _is_parttime_only_page(html) is False


def test_full_time_wins_when_duration_also_mentions_part_time_equivalent() -> None:
    assert (
        _infer_study_load_from_text("2 years full-time or part-time equivalent")
        == "Full Time"
    )
    assert (
        _infer_study_load_from_text("3 years, or part-time equivalent")
        == "Full Time"
    )
    assert (
        _infer_study_load_from_text("3 years (or part-time equivalent)")
        == "Full Time"
    )


def test_unisq_parenthesised_part_time_equivalent_is_not_part_time_only() -> None:
    html = """
    <ul class="details-listing">
      <li>
        <span class="details-listing__title">Duration</span>
        <span class="details-listing__value">
          3 years (or part-time equivalent)
        </span>
      </li>
    </ul>
    """
    assert _duration_labeled_values(html) == [
        "3 years (or part-time equivalent)"
    ]
    assert _is_parttime_only_page(html) is False


def test_explicit_part_time_only_wording_overrides_equivalent_full_time_measure() -> None:
    assert (
        _infer_study_load_from_text(
            "Duration 1 year equivalent full-time study. Only available part-time."
        )
        == "Part Time"
    )


def test_uow_duration_row_is_not_part_time_only_when_full_time_is_offered() -> None:
    html = """
    <div class="cf-college-info__row">
      <div class="cf-college-info__left"><span>Duration</span></div>
      <div class="cf-college-info__right">
        2 years full-time or part-time equivalent
      </div>
    </div>
    """
    assert _is_parttime_only_page(html) is False


def test_nested_duration_label_reads_its_list_item_value() -> None:
    html = """
    <ul class="details-listing">
      <li>
        <span class="details-listing__title"><strong>Duration</strong></span>
        <span class="details-listing__value">
          3 years full-time or equivalent part-time
        </span>
      </li>
    </ul>
    """
    assert (
        _infer_study_load_from_text(" ".join(_duration_labeled_values(html)))
        == "Full Time"
    )


def test_part_time_only_duration_is_globally_rejected() -> None:
    html = """
    <div class="cf-college-info__row">
      <div class="cf-college-info__left"><span>Duration</span></div>
      <div class="cf-college-info__right">2 years part-time</div>
    </div>
    """
    assert _parttime_only_filter_enabled() is True
    assert _is_parttime_only_page(html) is True

    accepted, reason = should_stage_course(
        "Master of Part-Time Study",
        {
            "course_name": "Master of Part-Time Study",
            "international_fee": 30000,
            "study_mode": "On Campus",
            "study_load": "Part Time",
        },
        source_url="https://example.edu/master-of-part-time-study",
    )
    assert accepted is False
    assert reason == "part_time_only"