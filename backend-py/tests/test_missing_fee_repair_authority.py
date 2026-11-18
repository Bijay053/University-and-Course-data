"""Missing-only repair must prove applicability, not fabricate a baseline."""
import pytest

from app.services.scraper.ai_extractor_run import apply_extraction_rules
from app.services.scraper.ai_repair_agent import (
    PatchValidationError, _validate_extraction_patch, validate_extraction_rule_on_samples,
)
from app.services.scraper.repair_fee_authority import FEE_EVIDENCE_GUARD, missing_fee_authority

RULE = {"css": "span.int", "regex": r"Tuition\s+\$([\d,.]+)",
        "regex_scope": "selected", "confidence": .95}


def course(name="Psychology", award="Bachelor of Science", amount="25,172.62"):
    return f"""<html><main><h1>{name}</h1><p>Earn your {award} with us.</p>
    <span class="int">Undergraduate Full Course Load Fees for International Students
    Tuition ${amount} Technology Fee $627.50 TOTAL $28,466.86</span>
    <div class="bed">Bachelor of Education Fees for International Students
    Tuition $26,190.44 Technology Fee $627.50 TOTAL $29,484.68</div>
    </main></html>"""


def sample(index, html=None, before=None):
    return {"url": f"https://university.example/courses/{index}",
            "html": html or course(), "before": before}


def test_twelve_blank_fees_can_prove_eleven_fills_without_fabricating_preservation():
    rows = [sample(i) for i in range(11)]
    rows.append(sample(11, course("Education", "Bachelor of Education")))
    report = validate_extraction_rule_on_samples("international_fee", RULE, rows)
    assert report["accepted"]
    assert report["missing_filled"] == 11
    assert report["populated_tested"] == report["regressions"] == 0
    assert report["scope_excluded"] == 1
    assert report["validation_basis"] == "guarded_course_owned_missing_fill"
    assert report["requires_live_validation"]
    assert report["rule"]["evidence_guard"] == FEE_EVIDENCE_GUARD
    assert "evidence_guard" not in RULE  # never mutate the model proposal
    excluded = report["samples"][-1]
    assert excluded["after"] is None and excluded["scope_excluded"]


def test_guard_is_retained_when_the_learned_rule_runs_on_future_courses():
    report = validate_extraction_rule_on_samples("international_fee", RULE, [sample(1), sample(2)])
    rule = report["rule"]
    _validate_extraction_patch("extraction_rules.international_fee", rule)
    good = apply_extraction_rules(course(), {"international_fee": rule})
    exception = apply_extraction_rules(course("Education", "Bachelor of Education"),
                                      {"international_fee": rule})
    assert good["international_fee"][0] == "25,172.62"
    assert exception["international_fee"] == (None, "ai_rule:fee_scope_unverified")


def test_education_requires_its_own_tuition_not_the_shared_undergraduate_amount():
    html = course("Education", "Bachelor of Education")
    assert not missing_fee_authority(html, 25172.62)["supported"]
    assert missing_fee_authority(html, 26190.44)["supported"]


@pytest.mark.parametrize("html", [
    "<main><h1>Psychology</h1><p>Bachelor of Science</p><span class='int'>Tuition $25,172.62</span></main>",
    "<nav><span class='int'>International tuition $25,172.62</span></nav><main><h1>Psychology</h1></main>",
    "<main><h1>Psychology</h1><span class='int'>Domestic and international tuition $25,172.62</span></main>",
    "<main><h1>Psychology</h1><span class='int'>International scholarship tuition $25,172.62</span></main>",
    "<main><h1>Psychology</h1><span class='int'>International tuition $12,000 Tuition $25,172.62</span></main>",
    course("Chemistry MSc", "Master of Science"),
    course("Chemistry PhD", "Doctor of Philosophy"),
    course().replace("<main>", "<section>").replace("</main>", "</section>"),
    course().replace("<h1>Psychology</h1>", ""),
    course().replace('<span class="int">', '<span class="int" hidden>'),
])
def test_no_blank_only_escape_for_unsupported_or_wrong_qualification_tuition(html):
    report = validate_extraction_rule_on_samples("international_fee", RULE, [sample(1, html), sample(2, html)])
    assert not report["accepted"]
    assert report["missing_filled"] == 0


def test_shared_fee_widget_is_not_evidence_for_every_programme():
    html = course("Education", "Bachelor of Education")
    assert not validate_extraction_rule_on_samples("international_fee", RULE,
        [sample(1, html), sample(2, html)])["accepted"]


def test_duplicate_url_does_not_supply_two_independent_positive_courses():
    assert not validate_extraction_rule_on_samples("international_fee", RULE,
        [sample(1), sample(1)])["accepted"]


def test_wrong_amount_and_changed_populated_fee_remain_rejected():
    guarded = {**RULE, "evidence_guard": FEE_EVIDENCE_GUARD}
    assert not missing_fee_authority(course(), 28466.86)["supported"]  # total, not tuition
    report = validate_extraction_rule_on_samples("international_fee", guarded,
        [sample(1), sample(2, before=30000)])
    assert not report["accepted"]
    assert report["regressions"] == 1


@pytest.mark.parametrize("field,guard", [
    ("international_fee", "accept_everything"),
    ("ielts_overall", FEE_EVIDENCE_GUARD),
])
def test_model_cannot_invent_a_guard(field, guard):
    with pytest.raises(PatchValidationError):
        _validate_extraction_patch("extraction_rules."+field, {**RULE, "evidence_guard": guard})


def test_course_owned_h2_is_supported_when_document_h1_lives_outside_main():
    html = course().replace("<h1>Psychology</h1>", "<h2>Psychology</h2>")
    html = html.replace("<main>", "<h1>University - Psychology</h1><main>")
    assert missing_fee_authority(html, 25172.62)["supported"]


def test_unknown_qualification_stays_unverified_not_inferred_from_shared_widget():
    html = course("Biology").replace("<p>Earn your Bachelor of Science with us.</p>",
                                    "<p>Explore biology in small classes.</p>")
    assert not missing_fee_authority(html, 25172.62)["supported"]


@pytest.mark.asyncio
async def test_snapshot_storage_returns_the_guarded_rule_not_the_original_proposal(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.services import snapshot_store
    from app.services.scraper.ai_repair_agent import _validate_extraction_patch_on_snapshots
    rows = [SimpleNamespace(course_url=sample(i)["url"], original_extraction={
        "international_fee": None, "audience_evidence": None}, storage_path=str(i))
        for i in range(2)]
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    monkeypatch.setattr(snapshot_store, "download_snapshot", AsyncMock(return_value=course().encode()))
    report = await _validate_extraction_patch_on_snapshots(
        "job", {"extraction_rules": {"international_fee": RULE}}, db)
    assert report["accepted"]
    assert report["rules"]["international_fee"]["evidence_guard"] == FEE_EVIDENCE_GUARD
    assert "evidence_guard" not in RULE


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_source", [False, True])
async def test_live_validation_requires_real_positive_outputs_and_reports_known_exceptions(monkeypatch, changed_source):
    from types import SimpleNamespace
    from app.services.scraper.ai_repair_live import LiveRepairEvidence
    rows = [sample(1), sample(2), sample(3, course("Education", "Bachelor of Education"))]
    replay = validate_extraction_rule_on_samples("international_fee", RULE, rows)
    cfg = SimpleNamespace(discovery=SimpleNamespace(allowed_extra_hostnames=[]),
                          extraction=SimpleNamespace(), filters=SimpleNamespace())
    evidence = LiveRepairEvidence({"university_id": 1, "scrape_url": rows[0]["url"],
                                  "effective_config": cfg, "repair_url_sample": []})
    monkeypatch.setattr(evidence, "discovery_validation", lambda *_: {"reasons": [], "courses": []})
    async def fetch(url):
        html = next(row["html"] for row in rows if row["url"] == url)
        if changed_source and url == rows[0]["url"]:
            html = html.replace("International Students", "Domestic Students")
        return {"classification": "course", "html": html, "owned_html": html, "url": url,
                "audience_evidence": {}}
    monkeypatch.setattr(evidence, "fetch", fetch)
    result = await evidence.validate({}, {}, {"extraction_rules": {
        "international_fee": replay["rule"]}}, {"reports": [replay]})
    assert result["accepted"] is not changed_source
    assert result["scope_exclusions"] == [{"url": rows[2]["url"], "field": "international_fee"}]
    if changed_source:
        assert any("not supported by course-owned live content" in s for s in result["reasons"])
