import asyncio
import re

import pytest

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
from app.services.scraper.guards import should_stage_course
from app.services.scraper.extractors import (
    duration,
    english_test,
    intake,
    location,
    study_mode,
)
from app.services.scraper.extractors.sit_html import (
    compact_course_html,
    current_course_campus_urls,
    has_current_course_panel,
    is_sit_course_url,
    merge_current_course_panels,
)
from app.services.scraper.pipelines.single_course import (
    _central_fee_match_has_usable_tuition,
    _restore_matching_static_duration_term,
)
from app.services.scraper.url_identity import (
    canonical_course_url_key,
    deduplicate_sit_course_urls,
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


def _sit_multi_campus_shell(name: str = "Graduate Diploma in Hotel Management") -> str:
    return f"""
    <html><head><title>{name} | SIT</title></head><body>
      <span id="courseName">{name}</span>
      <a href="/campus/Invercargill">global campus navigation</a>
      <a href="/programme/course/{name}/campus/Invercargill">Invercargill</a>
      <a href="/programme/course/{name}/campus/Queenstown">Queenstown</a>
      <div class="CourseInfo CourseSummary"></div>
    </body></html>
    """


def _sit_campus_html(
    campus: str,
    *,
    name: str = "Graduate Diploma in Hotel Management",
    intake: str = "February",
) -> str:
    return f"""
    <html><head><title>{name} | SIT</title></head><body>
      <span id="courseName">{name}</span>
      <div class="CourseInfo CourseSummary">
        <span id="currentCampusName">{campus}</span>
        <div class="keyInfoPane"><div class="row no-gutters">
          <div>Qualification:</div><div>Graduate Diploma</div>
          <div>Level:</div><div>7</div>
          <div>Duration:</div><div>One year full-time</div>
          <div>Study Modes:</div><div>On Campus</div>
        </div></div>
        <div class="lightGrey_bg_1 mb-4">
          Semester 1: 15 {intake} to 25 June 2027
        </div>
        <div id="headerApplicationCriteria_123">
          International applicants require IELTS 6.0 and PTE Academic 50.
        </div>
      </div>
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


def test_sit_compacted_page_extracts_hyphenated_week_duration():
    html = _sit_html().replace(
        "Three years full-time",
        "17-weeks full time",
    )
    compacted = compact_course_html(html)

    result = _run(duration.extract(compacted, "https://www.sit.ac.nz/x"))[0]

    assert result.value == 4.0
    assert result.normalized["duration_term"] == "Month"


def test_sit_compaction_preserves_up_to_full_time_duration():
    html = _sit_html().replace(
        "Three years full-time",
        "Up to 18 months full-time",
    )
    compacted = compact_course_html(html)

    assert "Up to 18 months full-time" in compacted
    result = _run(duration.extract(compacted, "https://www.sit.ac.nz/x"))[0]
    assert result.value == 18.0
    assert result.normalized["duration_term"] == "Month"


def test_sit_compaction_uses_intake_start_dates_not_end_dates():
    html, replacements = re.subn(
        r"Dates:.*?Fees:",
        "Dates: 2027 Intake 1: 15 February to 25 June 2027 "
        "Intake 2: 27 April to 3 September 2027 "
        "Intake 3: 12 July to 19 November 2027 Fees:",
        _sit_html(),
        flags=re.S,
    )
    assert replacements == 1
    compacted = compact_course_html(html)
    assert _run(intake.extract(compacted, "https://www.sit.ac.nz/x"))[0].value == [
        "February",
        "April",
        "July",
    ]


def test_sit_compaction_keeps_only_latest_year_rolling_intakes():
    html, replacements = re.subn(
        r"Dates:.*?Fees:",
        "Dates: 2026 Intake 4: 14 September to 11 July "
        "2026 Intake 5: 23 November to 19 September "
        "2027 Intake 1: 01 March to 28 November "
        "2027 Intake 2: 10 May to 05 March "
        "2027 Intake 3: 19 July to 05 May "
        "2027 Intake 4: 27 September to 14 July "
        "2027 Intake 5: 06 December to 22 September Fees:",
        _sit_html(),
        flags=re.S,
    )
    assert replacements == 1

    compacted = compact_course_html(html)

    assert _run(intake.extract(compacted, "https://www.sit.ac.nz/x"))[0].value == [
        "March",
        "May",
        "July",
        "September",
        "December",
    ]


def test_sit_title_only_shell_has_no_current_course_panel():
    html = "<html><h1>Master of Applied Management</h1><div class='CourseInfo CourseSummary'></div></html>"
    assert has_current_course_panel(html) is False


def test_sit_multi_campus_recovery_uses_only_current_course_links():
    name = "Graduate Diploma in Hotel Management"
    url = f"https://www.sit.ac.nz/Programme/Course/{name}"

    assert current_course_campus_urls(_sit_multi_campus_shell(name), url) == [
        f"https://www.sit.ac.nz/programme/course/{name}/campus/Invercargill",
        f"https://www.sit.ac.nz/programme/course/{name}/campus/Queenstown",
    ]


def test_sit_multi_campus_recovery_merges_verified_panels():
    merged = merge_current_course_panels(
        _sit_multi_campus_shell(),
        [
            _sit_campus_html("Invercargill", intake="February"),
            _sit_campus_html("Queenstown", intake="July"),
            _sit_campus_html("Unrelated", name="Bachelor of Unrelated"),
        ],
    )
    compacted = compact_course_html(merged)

    assert has_current_course_panel(merged) is True
    assert "Invercargill, Queenstown" in compacted
    assert _run(duration.extract(compacted, "https://www.sit.ac.nz/x"))[0].value == 1.0
    assert _run(intake.extract(compacted, "https://www.sit.ac.nz/x"))[0].value == [
        "February",
        "July",
    ]
    assert "Bachelor of Unrelated" not in compacted


def test_sit_multi_campus_recovery_prefers_current_rolling_intake_schedule():
    rolling_dates = (
        "2026 Intake 4: 14 September to 11 July "
        "2026 Intake 5: 23 November to 19 September "
        "2027 Intake 1: 01 March to 28 November "
        "2027 Intake 2: 10 May to 05 March "
        "2027 Intake 3: 19 July to 05 May "
        "2027 Intake 4: 27 September to 14 July "
        "2027 Intake 5: 06 December to 22 September"
    )
    queenstown = _sit_campus_html("Queenstown")
    queenstown, replacements = re.subn(
        r"Semester 1:.*?</div>",
        rolling_dates + "</div>",
        queenstown,
        flags=re.S,
    )
    assert replacements == 1
    merged = merge_current_course_panels(
        _sit_multi_campus_shell(),
        [
            _sit_campus_html("Invercargill", intake="February"),
            queenstown,
        ],
    )

    compacted = compact_course_html(merged)

    assert _run(intake.extract(compacted, "https://www.sit.ac.nz/x"))[0].value == [
        "March",
        "May",
        "July",
        "September",
        "December",
    ]


def test_sit_recovery_prefers_physical_route_and_full_time_duration():
    name = "Master of Applied Management"
    merged = merge_current_course_panels(
        _sit_multi_campus_shell(name),
        [
            _sit_campus_html("SIT Online", name=name).replace(
                "One year full-time",
                "18 months full-time. Up to five years part-time.",
            ).replace("On Campus", "Distance Learning"),
            _sit_campus_html("Invercargill / Hyflex", name=name).replace(
                "One year full-time",
                "18 months full-time, Up to five years part-time",
            ).replace("On Campus", "Onsite or Flexible Distance"),
        ],
    )
    compacted = compact_course_html(merged)

    assert "<dd>Invercargill</dd>" in compacted
    assert "SIT Online" not in compacted
    assert "five years part-time" not in compacted
    duration_result = _run(duration.extract(compacted, "https://www.sit.ac.nz/x"))[0]
    assert duration_result.value == 18.0
    assert duration_result.normalized["duration_term"] == "Month"
    assert _run(study_mode.extract(compacted, "https://www.sit.ac.nz/x"))[0].value == (
        "Blended"
    )


@pytest.mark.asyncio
async def test_sit_schedule_listed_shell_recovers_before_extraction(monkeypatch):
    from app.services.scraper.config import set_uni_config
    from app.services.ai import gemini_client
    from app.services.scraper.pipelines import single_course

    cfg = load_uni_config(
        slug="sit",
        scrape_url="https://www.sit.ac.nz",
        university_id=67,
        name="Southern Institute of Technology",
    )
    set_uni_config(cfg)
    fetched: list[str] = []

    async def fake_fetch(url: str, **_kwargs) -> str:
        fetched.append(url)
        campus = "Queenstown" if "Queenstown" in url else "Invercargill"
        intake_month = "July" if campus == "Queenstown" else "February"
        return _sit_campus_html(campus, intake=intake_month)

    monkeypatch.setattr(single_course, "fetch_html", fake_fetch)
    async def fail_gemini(*_args, **_kwargs):
        raise AssertionError("complete deterministic SIT facts must skip Gemini")

    monkeypatch.setattr(gemini_client, "generate", fail_gemini)
    result = await single_course.extract_course(
        "https://www.sit.ac.nz/Programme/Course/Graduate Diploma in Hotel Management",
        country="New Zealand",
        html=_sit_multi_campus_shell(),
        use_ai_fallback=True,
        central_data={
            "fees": [
                {
                    "program_pattern": "Graduate Diploma in Hotel Management",
                    "international_fee": 19_000,
                    "currency": "NZD",
                    "fee_period": "Annual",
                }
            ],
            "fee_page_url": (
                "https://www.sit.ac.nz/Fees-Enrolments/International-Fees"
            ),
        },
    )

    assert result.get("error") is None
    assert len(fetched) == 2
    assert result["payload"]["international_fee"] == 19_000
    assert result["payload"]["course_location"] == "Invercargill, Queenstown"
    assert result["payload"]["duration"] == 1.0
    assert result["payload"]["intake_months"] == ["February", "July"]
    fee_evidence = [
        item
        for item in result["evidence"]
        if item["field_key"] == "international_fee"
        and item["method"] == "central_page:fees:exact"
    ]
    assert len(fee_evidence) == 1
    assert fee_evidence[0]["source_url"] == (
        "https://www.sit.ac.nz/Fees-Enrolments/International-Fees"
    )


@pytest.mark.asyncio
async def test_sit_exact_fee_does_not_hide_missing_required_location(monkeypatch):
    from app.services.ai import gemini_client
    from app.services.scraper import gemini_gate
    from app.services.scraper.config import set_uni_config
    from app.services.scraper.pipelines import single_course

    cfg = load_uni_config(
        slug="sit",
        scrape_url="https://www.sit.ac.nz",
        university_id=67,
        name="Southern Institute of Technology",
    )
    set_uni_config(cfg)
    calls: list[str] = []

    async def record_gemini(*_args, **_kwargs):
        calls.append("gemini")
        return gemini_client.GeminiResponse(
            "",
            0,
            0,
            0.0,
            skipped=True,
            skip_reason="test",
        )

    monkeypatch.setattr(gemini_client, "generate", record_gemini)
    monkeypatch.setattr(
        gemini_gate,
        "required_course_fields_complete",
        lambda _payload: False,
    )
    monkeypatch.setattr(
        single_course,
        "required_course_fields_complete",
        lambda _payload: False,
    )
    result = await single_course.extract_course(
        "https://www.sit.ac.nz/Programme/Course/Bachelor of Testing",
        country="New Zealand",
        html=_sit_html(),
        use_ai_fallback=False,
        central_data={
            "fees": [
                {
                    "program_pattern": "Bachelor of Testing",
                    "international_fee": 19_000,
                    "currency": "NZD",
                    "per": "Annual",
                }
            ],
            "fee_page_url": (
                "https://www.sit.ac.nz/Fees-Enrolments/International-Fees"
            ),
        },
    )

    assert calls == ["gemini"]
    assert result["payload"]["international_fee"] == 19_000


@pytest.mark.asyncio
async def test_sit_unlisted_shell_is_not_recovered(monkeypatch):
    from app.services.scraper.config import set_uni_config
    from app.services.scraper.pipelines import single_course

    cfg = load_uni_config(
        slug="sit",
        scrape_url="https://www.sit.ac.nz",
        university_id=67,
        name="Southern Institute of Technology",
    )
    set_uni_config(cfg)

    async def fail_fetch(*_args, **_kwargs) -> str:
        raise AssertionError("an unlisted SIT shell must not trigger recovery")

    monkeypatch.setattr(single_course, "fetch_html", fail_fetch)
    result = await single_course.extract_course(
        "https://www.sit.ac.nz/Programme/Course/Graduate Diploma in Hotel Management",
        country="New Zealand",
        html=_sit_multi_campus_shell(),
        use_ai_fallback=False,
        central_data={
            "fees": [
                {
                    "program_pattern": "Bachelor of Another Programme",
                    "international_fee": 19_000,
                }
            ]
        },
    )

    assert result["error"] == "skipped:sit_course_panel_missing"
    assert result["_perf"]["sit_panel_recovery_attempted"] is False


@pytest.mark.asyncio
async def test_sit_missing_schedule_defers_panel_recovery(monkeypatch):
    from app.services.scraper.config import set_uni_config
    from app.services.scraper.pipelines import single_course

    cfg = load_uni_config(
        slug="sit",
        scrape_url="https://www.sit.ac.nz",
        university_id=67,
        name="Southern Institute of Technology",
    )
    set_uni_config(cfg)

    async def fail_fetch(*_args, **_kwargs) -> str:
        raise AssertionError("recovery must wait for the required fee schedule")

    monkeypatch.setattr(single_course, "fetch_html", fail_fetch)
    result = await single_course.extract_course(
        "https://www.sit.ac.nz/Programme/Course/Graduate Diploma in Hotel Management",
        country="New Zealand",
        html=_sit_multi_campus_shell(),
        use_ai_fallback=False,
        central_data={"fees": []},
    )

    assert result["error"] == "central_fee_schedule_unavailable"
    assert result["_perf"]["sit_panel_recovery_deferred"] is True


@pytest.mark.asyncio
async def test_sit_stalled_panel_recovery_cancels_and_keeps_shell_skipped(
    monkeypatch,
):
    from app.services.scraper.config import set_uni_config
    from app.services.scraper.pipelines import single_course

    cfg = load_uni_config(
        slug="sit",
        scrape_url="https://www.sit.ac.nz",
        university_id=67,
        name="Southern Institute of Technology",
    )
    set_uni_config(cfg)
    cancelled: list[str] = []

    async def stalled_fetch(url: str, **_kwargs) -> str:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(url)

    monkeypatch.setattr(single_course, "fetch_html", stalled_fetch)
    monkeypatch.setattr(single_course, "clamp_timeout", lambda _seconds: 1.0)
    result = await single_course.extract_course(
        "https://www.sit.ac.nz/Programme/Course/Graduate Diploma in Hotel Management",
        country="New Zealand",
        html=_sit_multi_campus_shell(),
        use_ai_fallback=False,
        central_data={
            "fees": [
                {
                    "program_pattern": "Graduate Diploma in Hotel Management",
                    "international_fee": 19_000,
                }
            ]
        },
    )

    assert result["error"] == "skipped:sit_course_panel_missing"
    assert result["_perf"]["sit_panel_recovery_attempted"] is True
    assert len(cancelled) == 2


def test_sit_url_identity_collapses_case_and_space_encoding_variants():
    assert canonical_course_url_key(
        "https://www.sit.ac.nz/Programme/Course/Bachelor of Commerce"
    ) == canonical_course_url_key(
        "https://www.sit.ac.nz/programme/course/Bachelor%20of%20Commerce"
    )

def test_sit_url_dedup_prefers_current_public_programme_url():
    public = {
        "name": "Bachelor of Commerce",
        "url": "https://www.sit.ac.nz/Programme/Course/Bachelor%20of%20Commerce",
    }
    aliases = [
        {
            "name": "Bachelor of Commerce",
            "url": "http://sit.ac.nz/programme/course/bachelor of commerce/",
        },
        public,
        {
            "name": "Bachelor of Commerce",
            "url": "https://www.sit.ac.nz/programme/course/BACHELOR%20OF%20COMMERCE",
        },
    ]

    kept, dropped = deduplicate_sit_course_urls(aliases)

    assert kept == [public]
    assert dropped == 2
def test_static_duration_unit_is_restored_when_numeric_value_is_unchanged():
    payload = {
        "duration": 8.0,
        "duration_term": "Graduate Diploma in Audio Production",
    }
    assert _restore_matching_static_duration_term(
        payload, [(8.0, "Month")]
    )
    assert payload == {"duration": 8.0, "duration_term": "Month"}


def test_static_duration_unit_does_not_override_a_replaced_duration():
    payload = {"duration": 1.0, "duration_term": "Year"}
    assert not _restore_matching_static_duration_term(
        payload, [(8.0, "Month")]
    )
    assert payload == {"duration": 1.0, "duration_term": "Year"}


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
    assert cfg.extraction.staging.skip_degree_qualifier_check is True
    assert cfg.extraction.fees.require_central_fee_match is True
    assert cfg.extraction.fees.force_central_fee_stage is False
    assert cfg.extraction.fees.currency_override == "NZD"
    assert "Direct Material Costs" in cfg.extraction.fees.reject_keywords
    assert cfg.extraction.fees.central_fee_course_aliases[
        "New Zealand Diploma in Audio Engineering and Production (Level 5)"
    ] == "New Zealand Diploma in Audio Engineering (Level 5)"


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


def test_sit_verified_aliases_preserve_exact_fee_matching():
    records = [
        {
            "program_pattern": "New Zealand Diploma in Audio Engineering (Level 5)",
            "international_fee": 19000.0,
            "currency": "NZD",
            "per": "Full Course",
        }
    ]
    aliases = {
        "New Zealand Diploma in Audio Engineering and Production (Level 5)":
            "New Zealand Diploma in Audio Engineering (Level 5)"
    }
    matched, kind = match_central_fee(
        "New Zealand Diploma in Audio Engineering and Production (Level 5)",
        records,
        exact_only=True,
        course_aliases=aliases,
    )
    assert matched is records[0]
    assert kind == "exact"

    unmatched, kind = match_central_fee(
        "New Zealand Diploma in Audio Engineering and Production (Level 4)",
        records,
        exact_only=True,
        course_aliases=aliases,
    )
    assert unmatched is None
    assert kind == "none"


def test_every_loaded_sit_fee_alias_resolves_exactly_without_crossing_awards():
    cfg = load_uni_config(
        slug="sit",
        scrape_url="https://www.sit.ac.nz",
        university_id=67,
        name="Southern Institute of Technology",
    )
    aliases = cfg.extraction.fees.central_fee_course_aliases
    schedule_html = """
    <table>
      <tr><th>Programmes</th><th>Duration</th><th>Tuition Fee</th>
        <th>Resource Fee</th><th>Total Fee (whole course)</th></tr>
      <tr><td>Graduate Diploma in Screen Arts (Filmmaking)</td><td>1 year</td><td>$19,000</td><td>$1,000</td><td>$20,000</td></tr>
      <tr><td>Graduate Diploma in Screen Arts (Visual Media)</td><td>1 year</td><td>$19,000</td><td>$1,000</td><td>$20,000</td></tr>
      <tr><td>Bachelor of Engineering Technology (Majors in Civil Engineering and Mechanical Engineering)</td><td>3 years</td><td>$19,500</td><td>$1,000</td><td>$20,500</td></tr>
      <tr><td>Bachelor of Screen Arts (with majors in Animation, Digital Content Creation, Film &amp; Game Design</td><td>3 years</td><td>$19,000</td><td>$1,000</td><td>$20,000</td></tr>
      <tr><td>Diploma in Agriculture (Level 5) (in collaboration with Massey University) only available at Telford specialist campus in Balclutha</td><td>1 year</td><td>$19,500</td><td>$1,000</td><td>$20,500</td></tr>
      <tr><td>New Zealand Diploma in Audio Engineering (Level 5)</td><td>1 year</td><td>$19,000</td><td>$1,000</td><td>$20,000</td></tr>
      <tr><td>New Zealand Diploma in Audio Engineering (Level 6)</td><td>1 year</td><td>$19,000</td><td>$1,000</td><td>$20,000</td></tr>
      <tr><td>New Zealand Diploma in Business (Level 5)</td><td>1 year</td><td>$19,000</td><td>$1,000</td><td>$20,000</td></tr>
      <tr><td>New Zealand Diploma in Enrolled Nursing (Level 5)</td><td>18 months</td><td>$29,000</td><td>$1,000</td><td>$30,000</td></tr>
      <tr><td>Diploma in Culinary Excellence (Level 5)</td><td>1 year</td><td>$19,000</td><td>$1,000</td><td>$20,000</td></tr>
      <tr><td>New Zealand Diploma in Architectural Technology (Level 6)</td><td>2 years</td><td>$19,000</td><td>$1,000</td><td>$20,000</td></tr>
      <tr><td>New Zealand Diploma in Engineering (Civil/Mechanical) (Level 6)</td><td>2 years</td><td>$19,500</td><td>$1,000</td><td>$20,500</td></tr>
      <tr><td>New Zealand Diploma in Veterinary Nursing (Companion Animals Veterinary Nursing) (Level 6)</td><td>2 years</td><td>$23,500</td><td>$1,000</td><td>$24,500</td></tr>
      <tr><td>New Zealand Certificate in Business (Small Business)</td><td>6 months</td><td>$9,250</td><td>$500</td><td>$9,750</td></tr>
      <tr><td>New Zealand Certificate Study and Employment Pathways (Nursing and Health)</td><td>6 months</td><td>$9,250</td><td>$500</td><td>$9,750</td></tr>
    </table>
    """
    records = _parse_fee_page_html(
        schedule_html,
        "https://www.sit.ac.nz/Fees-Enrolments/International-Fees",
    )
    expected_fees = {
        record["program_pattern"]: record["international_fee"]
        for record in records
    }
    assert len(aliases) == 24
    assert len(records) == 15
    for source, target in aliases.items():
        matched, kind = match_central_fee(
            source,
            records,
            exact_only=True,
            course_aliases=aliases,
        )
        assert matched is not None, source
        assert matched["program_pattern"] == target
        assert matched["international_fee"] == expected_fees[target]
        assert matched["international_fee"] > 0
        assert kind == "exact"


def test_central_fee_alias_rejects_cross_award_mapping():
    records = [
        {
            "program_pattern": "New Zealand Diploma in Testing (Level 5)",
            "international_fee": 19000.0,
        }
    ]
    matched, kind = match_central_fee(
        "New Zealand Certificate in Testing (Level 4)",
        records,
        exact_only=True,
        course_aliases={
            "New Zealand Certificate in Testing (Level 4)":
                "New Zealand Diploma in Testing (Level 5)"
        },
    )
    assert matched is None
    assert kind == "none"


def test_central_fee_alias_rejects_cross_level_mapping():
    records = [
        {
            "program_pattern": "New Zealand Certificate in Testing (Level 5)",
            "international_fee": 19000.0,
        }
    ]
    matched, kind = match_central_fee(
        "New Zealand Certificate in Testing (Level 4)",
        records,
        exact_only=True,
        course_aliases={
            "New Zealand Certificate in Testing (Level 4)":
                "New Zealand Certificate in Testing (Level 5)"
        },
    )
    assert matched is None
    assert kind == "none"


def test_sit_online_only_programme_remains_globally_rejected():
    accepted, reason = should_stage_course(
        "New Zealand Certificate in Testing",
        {
            "course_name": "New Zealand Certificate in Testing",
            "international_fee": 19000.0,
            "study_mode": "Online",
            "online_only": True,
        },
        "https://www.sit.ac.nz/Programme/Course/New Zealand Certificate in Testing",
    )
    assert accepted is False
    assert reason == "online_only"


def test_unlisted_sit_certificate_cannot_receive_an_exact_fee():
    matched, kind = match_central_fee(
        "New Zealand Certificate in Unlisted Subject",
        [
            {
                "program_pattern": "New Zealand Certificate in Listed Subject",
                "international_fee": 19000.0,
            }
        ],
        exact_only=True,
        course_aliases={},
    )
    assert matched is None
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

def test_sit_url_dedup_preserves_distinct_programmes_and_non_sit_urls():
    items = [
        {"url": "https://www.sit.ac.nz/Programme/Course/Bachelor%20of%20Commerce"},
        {"url": "https://www.sit.ac.nz/Programme/Course/Bachelor%20of%20Nursing"},
        {"url": "https://example.edu/Programme/Course/Bachelor%20of%20Commerce"},
    ]

    kept, dropped = deduplicate_sit_course_urls(items)

    assert kept == items
    assert dropped == 0
