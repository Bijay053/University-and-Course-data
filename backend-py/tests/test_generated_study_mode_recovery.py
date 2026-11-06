"""Generated rules must not mistake descriptions for usable delivery proof.

All HTML is local. Provider calls and database sessions are test doubles; no
operator rows, network requests, credentials, or paid services are used.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.scraper.ai_extractor_run import apply_extraction_rules, should_skip_gemini
from app.services.scraper.recipe_rules import apply_recipe_rules
from app.study_modes import normalize_study_mode, sanitize_study_mode_payload
from tests.test_study_mode_semantics import PROSE, VALID

DETAILED = VALID[-2]
URL = "https://mode-contract.example/courses/bachelor-of-testing"


@pytest.fixture
def pipeline_context(monkeypatch):
    from app.services.scraper.config.context import current_uni_config
    from app.services.scraper.config.schema import UniConfig
    from app.services.scraper.pipelines import single_course
    from app.services.scraper.extractors import gemini_primary
    from app.services.ai import gemini_client

    config = UniConfig(
        slug="mode-contract", name="Mode Contract University",
        base_url="https://mode-contract.example", scrape_url=URL,
        extraction={"skip_per_course_browser": True},
    )
    token = current_uni_config.set(config)
    no_fetch = AsyncMock(side_effect=AssertionError("Unexpected network fetch"))
    monkeypatch.setattr(single_course, "fetch_html", no_fetch)
    monkeypatch.setattr(gemini_primary, "extract_primary", AsyncMock(
        return_value=({}, 0.0, 0, 0, {"skipped": True, "skip_reason": "test"}),
    ))
    monkeypatch.setattr(gemini_client, "generate", AsyncMock(return_value=SimpleNamespace(
        text="{}", cost_usd=0.0, input_tokens=0, output_tokens=0,
    )))
    try:
        yield config
        no_fetch.assert_not_awaited()
    finally:
        current_uni_config.reset(token)


@pytest.mark.parametrize("bad", [PROSE, "Online learning equips students with knowledge and practical skills."])
def test_contaminated_selector_keeps_raw_candidate_but_not_coverage(bad, caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        results = apply_extraction_rules(
            f'<p class="intro">{bad}</p>', {"study_mode": {"css": ".intro"}},
        )
    assert results["study_mode"] == (bad, "ai_rule:css")
    assert "0/1 rules fired" in caplog.text
    # Exactly at the threshold with a real mode, below it with prose.
    fields = ["study_mode", *[f"other_{i}" for i in range(12)]]
    results.update({f: ("filled", "ai_rule:css") for f in fields[1:12]})
    assert not should_skip_gemini(results, fields)
    assert should_skip_gemini({**results, "study_mode": (DETAILED, "ai_rule:css")}, fields)


@pytest.mark.parametrize("mode", [DETAILED, "On Campus, Online", "Part-time only"])
def test_detailed_generated_labels_still_cover_the_field(mode):
    results = apply_extraction_rules(f"<div>{mode}</div>", {"study_mode": {"css": "div"}})
    assert should_skip_gemini(results, ["study_mode"])
    assert results["study_mode"][0] == mode


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_mode", [PROSE, "Online learning equips students with knowledge and practical skills."])
@pytest.mark.parametrize(
    ("recovered", "study_load", "authoritative_online_only"),
    [
        (DETAILED, "Full Time", False), ("On Campus, Online", "Full Time", False),
        ("On Campus", "Full Time", False), ("Online", "Full Time", False),
        ("On Campus", "Full Time", True), (None, "Part Time", False),
    ],
)
async def test_real_pipeline_retries_contaminated_mode_without_losing_evidence(
    monkeypatch, pipeline_context, recovered, study_load, raw_mode, authoritative_online_only,
):
    from app.services.scraper.extractors import study_mode
    from app.services.scraper.extractors.base import ExtractionResult
    from app.services.scraper.pipelines import single_course
    from app.services.scraper.guards import should_stage_course

    fallback = AsyncMock(return_value=[ExtractionResult(
        field_key="study_mode", value=recovered,
        normalized={
            "study_load": study_load, **({"study_mode": recovered} if recovered else {}),
            **({"online_only": True} if authoritative_online_only else {}),
        },
        method="study_mode:label", confidence=0.95, snippet=recovered or "Part Time",
    )])
    monkeypatch.setattr(study_mode, "extract", fallback)
    html = (
        f"<html><body><h1>Bachelor of Testing</h1><p class='intro'>{raw_mode}</p>"
        "<p>Duration: 3 years. Location: Sydney. IELTS 6.0. Intake: February.</p>"
        "</body></html>"
    )
    result = await single_course.extract_course(
        URL, html=html, use_ai_fallback=False,
        extraction_rules={"study_mode": {"css": ".intro"}},
    )
    assert not result.get("error"), result
    fallback.assert_awaited_once()
    payload, evidence = result["payload"], result["evidence"]
    assert payload["study_load"] == study_load
    rejected = [e for e in evidence if e.get("field_key") == "study_mode" and e.get("value") == raw_mode]
    assert len(rejected) == 1
    assert rejected[0]["selected"] is False
    assert rejected[0]["decision_status"] == "needs_review"
    assert rejected[0]["validation_status"] == "invalid"
    assert normalize_study_mode(payload.get("study_mode")) == recovered
    if recovered is not None:
        assert payload["study_mode"] == recovered
        if recovered == "Online":
            # Confirmed online-only courses intentionally exit before final
            # selection/staging; their authoritative source must still remain.
            assert result["_perf"]["ai_skipped_online_only"] is True
            assert any(e.get("value") == recovered and e.get("method") == "study_mode:label" for e in evidence)
        else:
            assert any(e.get("value") == recovered and e.get("decision_status") == "selected" for e in evidence)
        assert payload["extraction_method"]["study_mode"] == "study_mode:label"
    else:
        # Missing recovery is not an invented On Campus value or false online
        # authority; the rejected raw candidate remains available in evidence.
        assert payload["study_mode"] is None
        cleaned = sanitize_study_mode_payload({**payload, "evidence": evidence})
        assert cleaned["study_mode"] is None
        assert "invalid_study_mode" in cleaned["scrape_warnings"]
        assert payload["extraction_method"]["study_mode"].endswith(":null")
    if authoritative_online_only or recovered == "Online":
        from app.services.scraper.guards import is_online_only_for_staging
        assert is_online_only_for_staging("Bachelor of Testing", payload, URL)
        assert should_stage_course("Bachelor of Testing", payload, URL) == (True, "accepted")
    elif study_load == "Part Time":
        from app.services.scraper.guards import is_part_time_only_for_staging
        assert is_part_time_only_for_staging(payload)
        assert should_stage_course("Bachelor of Testing", payload, URL) == (True, "accepted")
    else:
        assert not payload.get("online_only")
        assert should_stage_course("Bachelor of Testing", payload, URL)[0] is True


def test_recipe_does_not_replace_rejected_mode_with_a_campus_default():
    raw = {"study_mode": PROSE, "course_location": "Sydney", "study_load": "Part Time"}
    apply_recipe_rules(raw, {"study_mode_from_location": True})
    assert raw["study_mode"] == PROSE
    assert raw["study_load"] == "Part Time"
    valid = {"study_mode": DETAILED, "course_location": "Sydney"}
    apply_recipe_rules(valid, {"study_mode_from_location": True})
    assert valid["study_mode"] == DETAILED
    exhausted = sanitize_study_mode_payload({**raw})
    apply_recipe_rules(exhausted, {"study_mode_from_location": True})
    assert exhausted["study_mode"] is None
    assert exhausted["study_load"] == "Part Time"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [PROSE, None, DETAILED, "On Campus, Online"])
@pytest.mark.parametrize("envelope", [False, True])
async def test_repair_scan_counts_only_semantically_valid_saved_modes(
    monkeypatch, pipeline_context, mode, envelope,
):
    from app.services.scraper import ai_repair_agent
    from app.services.scraper.config import loader
    from app.services.scraper.pipelines import single_course

    current = SimpleNamespace(
        course_website=URL, study_mode=None, international_fee=10000,
        ielts_overall=6, status="pending", fee_scope_key=None, extraction_method={},
        course_name="Bachelor of Testing", study_load=None,
    )
    monkeypatch.setattr(loader, "load_uni_config", lambda **_: pipeline_context)
    payload = {"study_mode": mode}
    monkeypatch.setattr(single_course, "extract_course", AsyncMock(
        return_value={"payload": payload, "evidence": []} if envelope else payload,
    ))
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[
            SimpleNamespace(scalar=lambda: 1),
            SimpleNamespace(first=lambda: ({},)),
            SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: [{"id": 1, "course_website": URL}])),
            SimpleNamespace(scalar_one_or_none=lambda: current),
        ]),
        commit=AsyncMock(),
    )
    fills = await ai_repair_agent._run_extraction_scan(
        {"university_id": 1, "job_id": "owned-test", "scrape_url": URL, "uni_name": "Mode Contract"},
        {}, db,
    )
    assert not fills.get("errors"), fills
    assert fills["courses_rescanned"] == 1
    assert fills["mode_fills"] == int(normalize_study_mode(mode) is not None)
    assert current.study_mode == normalize_study_mode(mode)
    assert db.commit.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["approved", "pending", "review"])
@pytest.mark.parametrize("envelope", [True, False])
@pytest.mark.parametrize(
    ("facts", "reason", "existing_facts"),
    [
        ({"study_mode": "Online"}, "online_only", {}),
        ({"study_mode": "On Campus", "online_only": True}, "online_only", {}),
        ({"study_mode": "On Campus", "study_load": "Part Time"}, "part_time_only", {}),
        ({"study_mode": "On Campus"}, "online_only", {"study_mode": "Online"}),
        ({"study_mode": None, "study_load": "Full Time"}, "part_time_only", {"study_load": "Part Time"}),
    ],
)
async def test_repair_scan_withholds_all_ineligible_updates(
    monkeypatch, pipeline_context, status, envelope, facts, reason, existing_facts,
):
    from app.services.scraper import ai_repair_agent
    from app.services.scraper.config import loader
    from app.services.scraper.pipelines import single_course

    current = SimpleNamespace(
        course_website=URL, course_name="Bachelor of Testing", study_mode=None,
        study_load=None, international_fee=None, ielts_overall=None,
        status=status, fee_scope_key=None, extraction_method={},
    )
    for key, value in existing_facts.items():
        setattr(current, key, value)
    before = vars(current).copy()
    fresh = {**facts, "international_fee": 25000, "ielts_overall": 7}
    monkeypatch.setattr(loader, "load_uni_config", lambda **_: pipeline_context)
    monkeypatch.setattr(single_course, "extract_course", AsyncMock(
        return_value={"payload": fresh, "evidence": []} if envelope else fresh,
    ))
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[
            SimpleNamespace(scalar=lambda: 1),
            SimpleNamespace(first=lambda: ({},)),
            SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: [{"id": 1, "course_website": URL}])),
            SimpleNamespace(scalar_one_or_none=lambda: current),
        ]),
        commit=AsyncMock(),
    )
    fills = await ai_repair_agent._run_extraction_scan(
        {"university_id": 1, "job_id": "owned-test", "scrape_url": URL, "uni_name": "Mode Contract"},
        {}, db,
    )
    assert fills["mode_fills"] == fills["fee_fills"] == fills["ielts_fills"] == 0
    assert len(fills["errors"]) == 1
    assert fills["errors"][0]["url"] == URL
    assert reason in fills["errors"][0]["reason"]
    # No newly ineligible mode, fee, or English value is written while the old
    # approved status remains. This is a blocked action, not a successful repair
    # or an automatic unpublish of previously approved operator data.
    assert vars(current) == before
