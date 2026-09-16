import asyncio

from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.orchestrator import (
    _apply_central_page_overrides,
    _extraction_failure_details,
    _inject_yaml_fee_page,
    _select_yaml_config,
)
from app.services.scraper.central_pages import (
    _cache_source_matches,
    _parse_fee_page_html,
    match_central_fee,
)
from app.services.scraper.extractors import (
    duration,
    english_test,
    intake,
    location,
    study_mode,
)
from app.services.scraper.extractors.sit_html import (
    compact_course_html,
    is_sit_course_url,
)
from app.services.scraper.pipelines.single_course import (
    _central_fee_match_has_usable_tuition,
)


def _run(coro):
    return asyncio.run(coro)


def _sit_html() -> str:
    return """
    <html><head><title>Bachelor of Testing | SIT</title></head><body>
      <header><span class="phone">0800 4 0 FEES (0800 4 0 3337)</span></header>
      <span id="courseName">Bachelor of Testing</span>
      <div class="CourseInfo CourseSummary">
        <span id="currentCampusName">Invercargill</span>
        <div class="keyInfoPane">
          <div class="row no-gutters">
            <div>Qualification:</div><div>Degree</div>
            <div>Level:</div><div>7</div>
            <div>Duration:</div><div>Three years full-time</div>
            <div>Study Modes:</div><div>On Campus</div>
          </div>
        </div>
        <div class="lightGrey_bg_1 mb-4">
          Dates: 2027 Semester 1: 15 February to 25 June 2027
          Semester 2: 12 July to 19 November 2027
          Fees: No Tuition Fees. Direct Material Costs: $1,577 per year.
          International Fees can be found here.</div>
        <div id="headerApplicationCriteria_123">
          International applicants require IELTS 6.0 and PTE Academic 50.
        </div>
      </div>
      <footer>Related course Online Auckland $99,999</footer>
    </body></html>
    """


def test_sit_course_compaction_keeps_course_owned_facts_only():
    assert is_sit_course_url(
        "https://www.sit.ac.nz/Programme/Course/Bachelor%20of%20Testing"
    )
    compacted = compact_course_html(_sit_html())
    assert "Bachelor of Testing" in compacted
    assert "Invercargill" in compacted
    assert "Three years full-time" in compacted
    assert "<dt>Study Modes</dt><dd>On Campus</dd>" in compacted
    assert "0800 4 0" not in compacted
    assert "Related course" not in compacted


def test_sit_compacted_page_extracts_location_mode_and_duration():
    compacted = compact_course_html(_sit_html())
    assert _run(location.extract(compacted, "https://www.sit.ac.nz/x"))[0].value == (
        "Invercargill"
    )
    assert _run(study_mode.extract(compacted, "https://www.sit.ac.nz/x"))[0].value == (
        "On Campus"
    )
    assert _run(duration.extract(compacted, "https://www.sit.ac.nz/x"))[0].value == 3.0
    assert _run(intake.extract(compacted, "https://www.sit.ac.nz/x"))[0].value == [
        "February",
        "July",
    ]
    english = _run(english_test.extract(compacted, "https://www.sit.ac.nz/x"))
    assert english[0].value == 6.0
    assert english[1].value == 50.0


def test_sit_yaml_uses_international_schedule_and_static_extraction():
    cfg = load_uni_config(
        slug="sit",
        scrape_url="https://www.sit.ac.nz",
        university_id=67,
        name="Southern Institute of Technology",
    )
    assert cfg.extraction.skip_per_course_browser is True
    assert cfg.extraction.max_parallel_fetch == 6
    assert cfg.discovery.allow_url_patterns == ["/Programme/Course/[^/?#]+"]
    assert cfg.discovery.course_detail_url_patterns == [
        "/Programme/Course/[^/?#]+"
    ]
    assert cfg.extraction.fees.central_fee_priority is True
    assert cfg.extraction.fees.central_fee_exact_match_only is True
    assert cfg.extraction.fees.require_central_fee_match is True
    assert cfg.extraction.fees.force_central_fee_stage is False
    assert cfg.extraction.fees.currency_override == "NZD"
    assert "Direct Material Costs" in cfg.extraction.fees.reject_keywords


def test_sit_yaml_applies_to_apex_and_www_hosts():
    for scrape_url in ("https://sit.ac.nz", "https://www.sit.ac.nz"):
        cfg = load_uni_config(
            slug="sit",
            scrape_url=scrape_url,
            university_id=67,
            name="Southern Institute of Technology",
        )
        assert cfg.extraction.fees.currency_override == "NZD"


def test_sit_central_schedule_uses_tuition_not_total_and_exact_award_matching():
    html = """
    <table>
      <tr><th>Programmes</th><th>Duration</th>
        <th>Tuition Fee (Scholarship incl.)</th><th>Resource Fee</th>
        <th>Total Fee (per year)</th></tr>
      <tr><td>Bachelor of Information Technology</td><td>3 years</td>
        <td>$19,000</td><td>$1,800</td><td>$20,800</td></tr>
      <tr><td>Postgraduate Diploma in Applied Management</td><td>1 year</td>
        <td>$21,500</td><td>$1,800</td><td>$23,300</td></tr>
    </table>
    """
    records = _parse_fee_page_html(
        html, "https://www.sit.ac.nz/Fees-Enrolments/International-Fees"
    )
    bit, kind = match_central_fee(
        "Bachelor of Information Technology", records, exact_only=True
    )
    assert kind == "exact"
    assert bit is not None
    assert bit["international_fee"] == 19000.0
    assert bit["per"] == "Annual"
    wrong_award, kind = match_central_fee(
        "Postgraduate Certificate in Applied Management",
        records,
        exact_only=True,
    )
    assert wrong_award is None
    assert kind == "none"


def test_sit_template_drift_returns_bounded_page_without_contact_chrome():
    html = """
    <html><head><title>Changed SIT template</title></head>
      <body><header>0800 4 0 FEES</header><h1>Bachelor of Testing</h1></body>
    </html>
    """
    compacted = compact_course_html(html)
    assert "Bachelor of Testing" in compacted
    assert "0800 4 0" not in compacted


def test_sit_central_schedule_outage_is_classified_for_recovery():
    details = _extraction_failure_details("central_fee_schedule_unavailable")
    assert details["reason"] == "central_fee_schedule_unavailable"
    assert details["retryable"] is True


def test_sit_required_schedule_replaces_stale_legacy_fee_page():
    cfg = load_uni_config(
        slug="sit",
        scrape_url="https://www.sit.ac.nz",
        university_id=67,
        name="Southern Institute of Technology",
    )
    effective = {
        "uniPages": {
            "feePage": "https://www.sit.ac.nz/International/How-to-Apply"
        }
    }

    assert _inject_yaml_fee_page(effective, cfg.extraction.fees) is True
    assert effective["uniPages"]["feePage"] == (
        "https://www.sit.ac.nz/Fees-Enrolments/International-Fees"
    )


def test_central_prefetch_uses_loaded_yaml_when_context_is_empty():
    loaded = load_uni_config(
        slug="sit",
        scrape_url="https://www.sit.ac.nz",
        university_id=67,
        name="Southern Institute of Technology",
    )
    selected = _select_yaml_config(None, loaded)
    effective: dict = {"uniPages": {}}
    assert _inject_yaml_fee_page(effective, selected.extraction.fees) is True
    applied = _apply_central_page_overrides(
        effective,
        {"feePage": "https://www.sit.ac.nz/International/How-to-Apply"},
        selected.extraction.fees,
    )
    assert applied == []
    assert effective["uniPages"]["feePage"] == (
        "https://www.sit.ac.nz/Fees-Enrolments/International-Fees"
    )


def test_sit_name_only_schedule_row_does_not_satisfy_fee_gate():
    name_only = {
        "program_pattern": "Bachelor of Information Technology",
        "international_fee": None,
    }
    assert _central_fee_match_has_usable_tuition(name_only, "exact") is False
    assert _central_fee_match_has_usable_tuition(
        {**name_only, "international_fee": 19000},
        "exact",
    ) is True


def test_sit_stale_central_cache_source_is_rejected():
    authoritative = "https://www.sit.ac.nz/Fees-Enrolments/International-Fees"
    assert _cache_source_matches(authoritative + "/", authoritative) is True
    assert _cache_source_matches(
        "https://www.sit.ac.nz/International/How-to-Apply",
        authoritative,
    ) is False