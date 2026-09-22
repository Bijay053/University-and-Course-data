"""Autonomous repair safety tests. All network, AI, Redis and DB IO is mocked."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services.scraper import ai_repair_agent as agent
from app.services.scraper import ai_repair_live as live
from app.services.scraper.config.context import current_uni_config


SEED = "https://university.example/courses"
ONE = SEED + "/bachelor-of-laws"
TWO = SEED + "/bachelor-of-science"
PARTNER = SEED + "/partners"


def course(title="Bachelor of Laws", extra=""):
    return f"""<html><main><h1>{title}</h1>
    <dl><dt>Award</dt><dd>Bachelor of Laws</dd>
    <dt>Duration</dt><dd>3 years</dd><dt>Study mode</dt><dd>Full time</dd></dl>
    <p class="fee">International tuition fee: <b>24000</b></p>
    <p class="ielts">IELTS overall: <b>6.5</b></p>{extra}</main></html>"""


LISTING = f"""<main><h1>Our courses</h1>
<a href="{ONE}">Bachelor of Laws</a><a href="{TWO}">Bachelor of Science</a></main>"""


def config():
    return SimpleNamespace(
        discovery=SimpleNamespace(allowed_extra_hostnames=[], insecure_tls_direct_hostnames=[]),
        extraction=SimpleNamespace(),
        filters=SimpleNamespace(),
    )


def context(**overrides):
    return {
        "job_id": "job-live", "university_id": 1, "uni_name": "University",
        "scrape_url": SEED, "effective_config": config(),
        "effective_discovery": {"allow_url_patterns": ["/broken/"]},
        "scrape_config_snapshot": {}, "admin_config": {},
        "filter_config_snapshot_present": False, "filter_config_snapshot": {},
        "raw_discovered": 10, "after_filter": 0, "imported": 0, "total_errors": 0,
        "drop_rate": 100, "passed_sample": [], "dropped_sample": [ONE, TWO],
        "repair_url_sample": [ONE, TWO], "quality": {}, "yaml_content": "",
        "yaml_snapshot": None, "yaml_file": None, "unis_dir": None,
        **overrides,
    }


@pytest.mark.parametrize("title", ["Bachelor of Laws", "Business", "MSc Clinical Practice"])
def test_legitimate_degree_survives_title_and_cpd_path(title):
    result = live.inspect_page("https://university.example/cpd/clinical-practice", course(title), config())
    assert result["classification"] == "course"
    assert result["fields"] and result["owned_html"]


@pytest.mark.parametrize("html,expected", [
    (LISTING, "listing"),
    ("<main><h1>Our partners</h1><p>Bachelor of Laws tuition and duration</p></main>", "non_course"),
    ("<main><h1>Terms and conditions</h1><p>Degree fee duration</p></main>", "non_course"),
    ("<main><h1>Bachelor of Laws</h1></main>", "unconfirmed"),
    ("<main><h1>Business</h1></main><footer>Award Bachelor Duration 3 years</footer>", "listing"),
    ("<main><h1>Bachelor of Laws</h1><p>Our award, duration, and fees.</p></main>", "unconfirmed"),
])
def test_non_course_and_title_only_evidence_not_accepted(html, expected):
    assert live.inspect_page(ONE, html, config())["classification"] == expected


def test_hidden_non_degree_and_footer_cannot_reject_degree():
    html = course(extra='<div hidden><dl><dt>Award</dt><dd>Short course</dd></dl></div>')
    html += "<footer>Domestic only. This is a short course.</footer>"
    assert live.inspect_page(ONE, html, config())["classification"] == "course"


def test_degree_title_with_owned_facts_survives_sibling_course_links():
    html = f"""<html><main>
    <h1>BSc (Hons) Accounting &amp; Finance</h1>
    <a href="{ONE}-with-foundation-year">Foundation Year option</a>
    <a href="{ONE}">In Clearing</a>
    <div class="detail">Study Mode Full-time</div>
    <div class="detail">Duration 3 years</div>
    </main></html>"""

    result = live.inspect_page(ONE, html, config())

    assert result["classification"] == "course"
    assert len(result["fields"]) == 2


@pytest.mark.parametrize("extra", [
    "<p>Domestic students only</p>",
    "<p>Study mode: Online only</p>",
    "<p>Duration: 3 years, part time only</p>",
])
def test_explicit_course_owned_ineligibility_is_not_repaired_away(extra):
    assert live.inspect_page(ONE, course(extra=extra), config())["classification"] == "ineligible"


@pytest.mark.asyncio
async def test_transport_failure_is_not_unpublished_and_config_context_restored(monkeypatch):
    previous = config()
    current_uni_config.set(previous)
    ctx = context()

    async def fetch(*_args):
        assert current_uni_config.get() is ctx["effective_config"]
        raise http_error

    http_error = ConnectionError("offline")
    monkeypatch.setattr(live, "_fetch_official", fetch)
    evidence = live.LiveRepairEvidence(ctx)
    result = await evidence.probe()
    assert result["status"] == "blocked" and result["accepted"] is False
    assert result["failures"] == 3
    assert all(s["classification"] == "network_failure" for s in result["samples"])
    assert current_uni_config.get() is previous


@pytest.mark.asyncio
async def test_configured_proxy_uses_existing_provider_bounded_no_retries(monkeypatch):
    from app.services import scraper_config_ai
    from app.services.scraper import http_fetcher
    cfg = config()
    cfg.extraction.scrape_do_static = True
    monkeypatch.setattr(scraper_config_ai, "_is_safe_public_url", lambda _url: (True, ""))
    provider = AsyncMock(return_value=course())
    monkeypatch.setattr(http_fetcher, "fetch_html_scrape_do", provider)
    html, failure, _ = await live._fetch_official(ONE, cfg, 20)
    assert html and not failure
    assert provider.await_args.kwargs["max_retries"] == 0
    assert provider.await_args.kwargs["request_timeout_seconds"] == 20
    assert provider.await_args.kwargs["render"] is False


@pytest.mark.asyncio
async def test_static_javascript_shell_retries_once_with_rendering(monkeypatch):
    from app.services import scraper_config_ai
    from app.services.scraper import http_fetcher

    cfg = config()
    cfg.extraction.scrape_do_static = True
    monkeypatch.setattr(scraper_config_ai, "_is_safe_public_url", lambda _url: (True, ""))
    shell = "<html><body><noscript><h1>JavaScript is disabled</h1></noscript></body></html>"
    provider = AsyncMock(side_effect=[shell, course()])
    monkeypatch.setattr(http_fetcher, "fetch_html_scrape_do", provider)

    html, failure, reason = await live._fetch_official(ONE, cfg, 20)

    assert html == course()
    assert not failure and not reason
    assert provider.await_count == 2
    first, second = provider.await_args_list
    assert first.kwargs["render"] is False
    assert second.kwargs["render"] is True
    assert first.kwargs["max_retries"] == second.kwargs["max_retries"] == 0
    assert 0 < second.kwargs["request_timeout_seconds"] <= 20


@pytest.mark.asyncio
async def test_rendered_javascript_shell_fails_closed(monkeypatch):
    from app.services import scraper_config_ai
    from app.services.scraper import http_fetcher

    cfg = config()
    cfg.extraction.scrape_do_static = True
    monkeypatch.setattr(scraper_config_ai, "_is_safe_public_url", lambda _url: (True, ""))
    shell = "<html><body><noscript><h1>JavaScript is disabled</h1></noscript></body></html>"
    provider = AsyncMock(side_effect=[shell, shell])
    monkeypatch.setattr(http_fetcher, "fetch_html_scrape_do", provider)

    html, failure, reason = await live._fetch_official(ONE, cfg, 20)

    assert not html
    assert failure == "challenge"
    assert "JavaScript-disabled shell" in reason
    assert provider.await_count == 2


def test_incidental_javascript_disabled_message_does_not_replace_real_page():
    html = course(extra="<noscript>JavaScript is disabled</noscript>")

    assert live._is_javascript_disabled_shell(html) is False
    assert live.inspect_page(ONE, html, config())["classification"] == "course"


@pytest.mark.asyncio
async def test_large_provider_html_keeps_bounded_course_evidence(monkeypatch):
    from app.services import scraper_config_ai
    from app.services.scraper import http_fetcher

    cfg = config()
    cfg.extraction.scrape_do_static = True
    monkeypatch.setattr(scraper_config_ai, "_is_safe_public_url", lambda _url: (True, ""))
    large = course() + ("x" * live._MAX_PROBE_HTML_BYTES)
    monkeypatch.setattr(
        http_fetcher, "fetch_html_scrape_do", AsyncMock(return_value=large)
    )

    html, failure, reason = await live._fetch_official(ONE, cfg, 20)

    assert not failure and not reason
    assert len(html.encode("utf-8")) <= live._MAX_PROBE_HTML_BYTES
    assert live.inspect_page(ONE, html, cfg)["classification"] == "course"


def test_bounded_html_does_not_split_multibyte_text():
    html = course(extra="<p>" + ("é" * live._MAX_PROBE_HTML_BYTES) + "</p>")

    bounded = live._bounded_html(html)

    assert len(bounded.encode("utf-8")) <= live._MAX_PROBE_HTML_BYTES
    assert "\ufffd" not in bounded
    assert live.inspect_page(ONE, bounded, config())["classification"] == "course"


@pytest.mark.asyncio
async def test_unsafe_host_is_audited_without_network(monkeypatch):
    fetch = AsyncMock()
    monkeypatch.setattr(live, "_fetch_official", fetch)
    evidence = live.LiveRepairEvidence(context())
    record = await evidence.fetch("http://127.0.0.1/private")
    assert record["classification"] == "unsafe_url"
    assert evidence.pages_checked == 0
    fetch.assert_not_called()


@pytest.mark.asyncio
async def test_page_and_elapsed_time_caps_are_hard_clamped(monkeypatch):
    fetch = AsyncMock(return_value=(course(), "", ""))
    monkeypatch.setattr(live, "_fetch_official", fetch)
    clock = [0.0]
    monkeypatch.setattr(live.time, "monotonic", lambda: clock[0])
    evidence = live.LiveRepairEvidence(context(), {
        "max_live_pages": 1000, "max_live_seconds": 1000, "fetch_timeout_seconds": 1000,
    })
    for _ in range(14):
        await evidence.fetch(ONE)
    assert fetch.await_count == 12 and evidence.pages_checked == 12
    assert evidence.max_seconds == 180 and evidence.fetch_seconds == 20
    evidence = live.LiveRepairEvidence(context())
    evidence.fetch_elapsed_seconds = 181
    assert (await evidence.fetch(ONE))["classification"] == "budget_exhausted"
    assert evidence.pages_checked == 0


@pytest.mark.asyncio
async def test_ai_deliberation_does_not_consume_live_fetch_budget(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(live.time, "monotonic", lambda: clock[0])

    async def fetch(*_args):
        clock[0] += 13
        return course(), "", ""

    monkeypatch.setattr(live, "_fetch_official", fetch)
    evidence = live.LiveRepairEvidence(context(), {"max_live_seconds": 180})
    await evidence.fetch(ONE)
    clock[0] += 120  # Simulated AI proposal time between live phases.

    result = await evidence.fetch(TWO)

    assert result["classification"] == "course"
    assert evidence.fetch_elapsed_seconds == 26


@pytest.mark.asyncio
async def test_validation_reuses_live_pages_across_repair_attempts(monkeypatch):
    fetch = AsyncMock(return_value=(course(), "", ""))
    monkeypatch.setattr(live, "_fetch_official", fetch)
    evidence = live.LiveRepairEvidence(context(effective_discovery={}))
    evidence.initial = {
        ONE: live.inspect_page(ONE, course(), config()),
        TWO: live.inspect_page(TWO, course(), config()),
    }

    first = await evidence.validate({}, {}, {})
    second = await evidence.validate({}, {}, {})

    assert first["accepted"] and second["accepted"]
    assert fetch.await_count == 2
    assert evidence.pages_checked == 2


@pytest.mark.asyncio
async def test_audience_repair_rejects_when_every_course_lacks_typed_evidence(
    monkeypatch,
):
    fetch = AsyncMock(return_value=(course(), "", ""))
    monkeypatch.setattr(live, "_fetch_official", fetch)
    evidence = live.LiveRepairEvidence(context(effective_discovery={}))
    evidence.initial = {
        ONE: live.inspect_page(ONE, course(), config()),
        TWO: live.inspect_page(TWO, course(), config()),
    }

    result = await evidence.validate({}, {}, {"audience_repair": True})

    assert result["accepted"] is False
    assert result["audience_proposals"] == []
    assert any("Audience evidence missing" in reason for reason in result["reasons"])


@pytest.mark.asyncio
async def test_invalid_filter_attempt_does_not_spend_validation_page_budget(monkeypatch):
    fetch = AsyncMock(return_value=(course(), "", ""))
    monkeypatch.setattr(live, "_fetch_official", fetch)
    evidence = live.LiveRepairEvidence(context(effective_discovery={}))
    evidence.initial = {ONE: live.inspect_page(ONE, course(), config())}

    result = await evidence.validate(
        {"allow_url_patterns": ["/courses/"]},
        {"allow_url_patterns": ["/does-not-match/"]},
        {},
    )

    assert not result["accepted"]
    fetch.assert_not_awaited()
    assert evidence.pages_checked == 0


@pytest.mark.asyncio
async def test_each_fetch_gets_actual_timeout(monkeypatch):
    monkeypatch.setattr(live, "_fetch_official", AsyncMock(return_value=(course(), "", "")))
    real_wait_for = asyncio.wait_for
    timeouts = []

    async def wait_for(coro, timeout):
        timeouts.append(timeout)
        return await real_wait_for(coro, timeout)

    monkeypatch.setattr(live.asyncio, "wait_for", wait_for)
    await live.LiveRepairEvidence(context(), {"fetch_timeout_seconds": 3}).fetch(ONE)
    assert timeouts == [3]


def test_preserve_known_courses_and_remove_contamination_not_raw_count():
    evidence = live.LiveRepairEvidence(context(passed_sample=[ONE, TWO, PARTNER], effective_discovery={}))
    evidence.initial = {
        ONE: live.inspect_page(ONE, course(), config()),
        TWO: live.inspect_page(TWO, course(), config()),
        PARTNER: live.inspect_page(PARTNER, "<main><h1>Our partners</h1></main>", config()),
    }
    assert evidence.discovery_needed({})
    assert not evidence.audit()["accepted"]
    good = evidence.discovery_validation({}, {"block_url_patterns": ["/partners$"]})
    assert good["accepted"] and good["preserved"] == sorted([ONE, TWO])
    assert good["rejected_removed"] == [PARTNER]
    bad = evidence.discovery_validation({}, {"allow_url_patterns": ["bachelor-of-laws"]})
    assert not bad["accepted"]
    assert any("lose known valid" in reason for reason in bad["reasons"])


@pytest.mark.asyncio
async def test_live_recheck_failure_blocks_apply_evidence(monkeypatch):
    evidence = live.LiveRepairEvidence(context())
    evidence.initial = {ONE: live.inspect_page(ONE, course(), config())}
    monkeypatch.setattr(live, "_fetch_official", AsyncMock(return_value=("", "challenge", "HTTP 403")))
    result = await evidence.validate({"allow_url_patterns": ["/broken/"]},
                                     {"allow_url_patterns": ["/courses/"]}, {})
    assert not result["accepted"]
    assert any("challenge" in reason for reason in result["reasons"])


@pytest.mark.parametrize("field,html", [
    ("international_fee", '<main><p>Domestic tuition fee: <b class="amount">24000</b></p></main>'),
    ("international_fee", '<main><p>International scholarship: <b class="amount">24000</b></p></main>'),
    ("international_fee", '<main><p>International application fee: <b class="amount">24000</b></p></main>'),
    ("ielts_overall", '<main><p>English level: <b class="amount">6.5</b></p></main>'),
])
def test_main_region_is_not_sufficient_field_authority(field, html):
    assert not live.field_authority_html(field, html)


def test_field_authority_keeps_explicit_international_tuition_and_test():
    assert "24000" in live.field_authority_html("international_fee", course())
    assert "6.5" in live.field_authority_html("ielts_overall", course())


def test_selector_evidence_preserves_same_panel_and_official_link():
    html = """<main><select id="audience">
      <option data-audience="Domestic" value="d">Domestic January 2027</option>
      <option data-audience="International" value="i"
              data-source="/requirements">International March 2027</option>
    </select></main>"""
    result = live.extract_audience_option_evidence(html, ONE, SEED)
    assert result["status"] == "accepted"
    assert result["same_panel"] and result["linked_official"]
    assert {row["audience"] for row in result["evidence"]} == {"domestic", "international"}


def test_selector_evidence_fails_closed_for_ambiguous_or_image_only_options():
    html = """<main><select>
      <option data-audience="Domestic International"></option>
    </select></main>"""
    result = live.extract_audience_option_evidence(html, ONE, SEED)
    assert result["status"] == "needs_review"
    assert result["evidence"] == []


def test_custom_listbox_identity_and_background_control_fail_closed():
    html = """<main>
      <div role="listbox" id="audience">
        <div role="option" data-audience="International">International</div>
        <div role="option"><span style="background-image:url(flag.png)"></span></div>
      </div>
      <div role="listbox" class="audience-background" style="background:url(flag.png)"></div>
    </main>"""
    result = live.extract_audience_option_evidence(html, ONE, SEED)
    assert result["status"] == "needs_review"


def test_linked_recipe_requires_official_source_and_balanced_audiences():
    from app.services.scraper.recipe_rules import build_audience_scoped_recipe_proposal
    evidence = {
        "status": "accepted", "same_panel": True, "linked_official": True,
        "evidence": [
            {"audience": "domestic", "intake_months": [1]},
            {"audience": "international", "intake_months": [3],
             "source_url": ONE, "source_official": True},
        ],
    }
    result = build_audience_scoped_recipe_proposal(evidence)
    assert result["status"] == "accepted"
    assert result["proposals"][0]["english"]["central_page"] == ONE
    evidence["evidence"].pop(0)
    assert build_audience_scoped_recipe_proposal(evidence)["status"] == "needs_review"


@pytest.mark.asyncio
@pytest.mark.parametrize("field,rule,extra", [
    ("international_fee", {"css": ".domestic b", "confidence": .95},
     '<p class="domestic">Domestic tuition fee: <b>24000</b></p>'),
    ("ielts_overall", {"css": ".grade b", "confidence": .95},
     '<p class="grade">Entry grade: <b>6.5</b></p>'),
])
async def test_live_selector_replay_rejects_wrong_authority_even_matching_number(monkeypatch, field, rule, extra):
    html = course(extra=extra)
    evidence = live.LiveRepairEvidence(context(effective_discovery={}))
    evidence.initial = {url: live.inspect_page(url, html, config()) for url in (ONE, TWO)}
    monkeypatch.setattr(live, "_fetch_official", AsyncMock(return_value=(html, "", "")))
    result = await evidence.validate({}, {}, {"extraction_rules": {field: rule}})
    assert result["accepted"] is False
    assert any("not supported" in reason for reason in result["reasons"])


@pytest.mark.asyncio
async def test_live_selector_replay_preserves_populated_baseline(monkeypatch):
    evidence = live.LiveRepairEvidence(context(effective_discovery={}))
    evidence.initial = {url: live.inspect_page(url, course(), config()) for url in (ONE, TWO)}
    monkeypatch.setattr(live, "_fetch_official", AsyncMock(return_value=(course(), "", "")))
    snapshots = {"reports": [{"field": "international_fee", "samples": [
        {"url": ONE, "before": None}, {"url": TWO, "before": 24000},
    ]}]}
    patch = {"extraction_rules": {"international_fee": {"css": ".fee b", "confidence": .95}}}
    assert (await evidence.validate({}, {}, patch, snapshots))["accepted"]
    snapshots["reports"][0]["samples"][1]["before"] = 25000
    result = await evidence.validate({}, {}, patch, snapshots)
    assert not result["accepted"]
    assert any("populated baseline" in reason for reason in result["reasons"])


def mock_loop(monkeypatch, ctx, ai):
    from app.services.ai import openai_client
    queued = {"university_id": 1, "session_id": "queued-id",
              "autonomous": {"enabled": True, "phase": "queued", "wrapper_marker": "keep",
                             "limits": {"max_attempts": 1}}}
    monkeypatch.setattr(agent, "read_session", lambda _job: queued)
    monkeypatch.setattr(agent, "_write_session", lambda *_args: None)
    monkeypatch.setattr(agent, "persist_repair_audit", AsyncMock())
    monkeypatch.setattr(agent, "_gather_context", AsyncMock(return_value=ctx))
    monkeypatch.setattr(agent, "_quality_snapshot", AsyncMock(return_value={}))
    monkeypatch.setattr(agent, "_predict_quality", AsyncMock(return_value=({}, {})))
    monkeypatch.setattr(openai_client, "chat_json", AsyncMock(return_value=ai))
    return openai_client.chat_json


class FakeDb:
    def __init__(self, cfg=None, rowcount=1):
        self.cfg = cfg or {}
        self.rowcount = rowcount
        self.writes = []
        self.commits = 0

    async def execute(self, statement, params):
        if str(statement).startswith("UPDATE"):
            self.writes.append(params)
        return SimpleNamespace(
            first=lambda: None,
            mappings=lambda: SimpleNamespace(first=lambda: {"scrape_config": self.cfg}),
            rowcount=self.rowcount,
        )

    async def rollback(self):
        pass

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [{}, {"autonomous": {"enabled": False}}])
async def test_durable_claim_keeps_live_gates_when_cache_is_lost_or_stale(monkeypatch, cached):
    chat = mock_loop(monkeypatch, context(), {"patches": []})
    monkeypatch.setattr(agent, "read_session", lambda _job: cached)
    fetch = AsyncMock(return_value=("", "challenge", "Access blocked"))
    monkeypatch.setattr(live, "_fetch_official", fetch)
    durable = {
        "job_id": "job-live", "session_id": "claimed-session", "university_id": 1,
        "autonomous": {"enabled": True, "worker_claim": "delivery", "limits": {"max_attempts": 1}},
    }
    result = await agent.run_ai_repair_loop(
        "job-live", FakeDb(), lease_token="claimed-session", durable_session=durable,
    )
    assert result["autonomous"]["enabled"] is True
    assert result["autonomous"]["worker_claim"] == "delivery"
    assert result["status"] == "failed"
    assert result["live_probe"]["accepted"] is False
    assert fetch.await_count > 0
    chat.assert_not_awaited()
    assert "phase" not in durable["autonomous"]  # Caller retains its durable fence.


@pytest.mark.asyncio
async def test_mismatched_durable_claim_fails_before_any_work(monkeypatch):
    write = Mock()
    monkeypatch.setattr(agent, "_write_session", write)
    with pytest.raises(ValueError, match="matching durable worker claim"):
        await agent.run_ai_repair_loop(
            "job-live", FakeDb(), lease_token="owner",
            durable_session={"job_id": "job-live", "session_id": "other"},
        )
    write.assert_not_called()


@pytest.mark.asyncio
async def test_loop_never_applies_when_fresh_live_verification_fails(monkeypatch):
    ctx = context()
    chat = mock_loop(monkeypatch, ctx, {
        "confidence": 95, "diagnosis": "Wrong filter", "patches": [
            {"section": "discovery", "field": "allow_url_patterns", "value": ["/courses/"]},
        ],
    })
    calls = []

    async def fetch(url, *_args):
        calls.append(url)
        if len(calls) > 3:
            return "", "network_failure", "timeout"
        return (LISTING if url == SEED else course()), "", ""

    monkeypatch.setattr(live, "_fetch_official", fetch)
    write = AsyncMock(side_effect=AssertionError("Must not apply"))
    monkeypatch.setattr(agent, "_apply_discovery_to_db", write)
    monkeypatch.setattr(agent, "_apply_to_yaml", lambda *_args, **_kwargs: pytest.fail("YAML changed"))
    result = await agent.run_ai_repair_loop("job-live", FakeDb())
    assert result["status"] == "failed"
    assert not result["live_probe"]["accepted"]
    assert result["attempts"][0]["live_validation"]["accepted"] is False
    assert result["autonomous"]["wrapper_marker"] == "keep"
    assert result["autonomous"]["phase"] == "repairing"
    assert result["max_attempts"] == 1
    assert chat.await_args.kwargs["max_tokens"] == 2048
    assert "BOUNDED LIVE EVIDENCE" in chat.await_args.kwargs["user"]
    write.assert_not_called()


@pytest.mark.asyncio
async def test_low_drop_contamination_routes_to_discovery_with_missing_run_snapshot(monkeypatch):
    ctx = context(passed_sample=[ONE, PARTNER], effective_discovery={},
                  drop_rate=0, after_filter=10, imported=5)
    chat = mock_loop(monkeypatch, ctx, {"confidence": 95, "patches": []})

    async def fetch(url, *_args):
        return (LISTING if url == SEED else "<main><h1>Our partners</h1></main>"
                if url == PARTNER else course()), "", ""

    monkeypatch.setattr(live, "_fetch_official", fetch)
    result = await agent.run_ai_repair_loop("job-live", FakeDb())
    assert "CURRENT FOCUS: Fix URL DISCOVERY" in chat.await_args.kwargs["user"]
    assert result["attempts"][0]["phase"] == "discovery"
    assert not result["live_probe"]["accepted"]


@pytest.mark.asyncio
async def test_actual_live_validation_allows_safe_filter_repair_without_legacy_snapshot(monkeypatch, tmp_path):
    ctx = context(yaml_file=tmp_path / "university.yaml", unis_dir=tmp_path)
    mock_loop(monkeypatch, ctx, {
        "confidence": 95, "patches": [
            {"section": "discovery", "field": "allow_url_patterns", "value": ["/courses/"]},
        ],
    })

    async def fetch(url, *_args):
        return (LISTING if url == SEED else course()), "", ""

    monkeypatch.setattr(live, "_fetch_official", fetch)
    monkeypatch.setattr(agent, "_assert_effective_discovery_patch", AsyncMock())
    db = FakeDb()
    result = await agent.run_ai_repair_loop("job-live", db)
    assert result["status"] == "completed", result.get("error")
    assert result["live_probe"]["accepted"]
    assert result["live_probe"]["status"] == "accepted"
    assert result["attempts"][0]["patch_applied_ok"]
    assert result["attempts"][0]["live_validation"]["accepted"]
    assert json.loads(db.writes[0]["expected"]) == {}
    assert not result["autonomous"]["verified"]
    assert "not a verified recovery" in result["final_verdict"]


@pytest.mark.asyncio
async def test_snapshot_validation_is_not_bypassed_by_live_evidence(monkeypatch):
    ctx = context(effective_discovery={}, imported=4, after_filter=4, drop_rate=0)
    mock_loop(monkeypatch, ctx, {
        "confidence": 95, "patches": [
            {"section": "recipe", "field": "extraction_rules.international_fee",
             "value": {"css": ".fee b", "confidence": .95}},
        ],
    })
    monkeypatch.setattr(live, "_fetch_official", AsyncMock(return_value=(course(), "", "")))
    monkeypatch.setattr(agent, "_read_scrape_config", AsyncMock(return_value={}))
    replay = AsyncMock(return_value={"accepted": False, "reports": [
        {"field": "international_fee", "rejection_reasons": ["No populated preservation baseline"]},
    ]})
    monkeypatch.setattr(agent, "_validate_extraction_patch_on_snapshots", replay)
    apply = AsyncMock(side_effect=AssertionError("No safe snapshot validation"))
    monkeypatch.setattr(agent, "_apply_recipe_to_db", apply)
    result = await agent.run_ai_repair_loop("job-live", FakeDb())
    replay.assert_awaited_once()
    apply.assert_not_called()
    assert not result["live_probe"]["accepted"]


@pytest.mark.asyncio
async def test_ai_call_has_actual_timeout_and_bounded_tokens(monkeypatch):
    ctx = context(effective_discovery={}, imported=4, after_filter=4, drop_rate=0)
    chat = mock_loop(monkeypatch, ctx, {"confidence": 95, "patches": []})
    monkeypatch.setattr(agent, "read_session", lambda _job: {
        "max_attempts": 2,
        "autonomous": {"enabled": True, "limits": {
            "max_attempts": 100, "ai_timeout_seconds": 100, "ai_max_tokens": 10000,
        }},
    })
    monkeypatch.setattr(live, "_fetch_official", AsyncMock(return_value=(course(), "", "")))
    actual_wait_for = asyncio.wait_for
    deadlines = []

    async def wait_for(coro, timeout):
        deadlines.append(timeout)
        return await actual_wait_for(coro, timeout)

    monkeypatch.setattr(agent.asyncio, "wait_for", wait_for)
    result = await agent.run_ai_repair_loop("job-live", FakeDb())
    assert 45 in deadlines and all(timeout <= 45 for timeout in deadlines)
    assert result["max_attempts"] == 2
    assert result["autonomous"]["limits"]["ai_max_tokens"] == 2048
    assert chat.await_args.kwargs["max_tokens"] == 2048


@pytest.mark.asyncio
async def test_discovery_cas_uses_exact_original_document(monkeypatch):
    before = {"admin_config": {"discovery": {"allow_url_patterns": ["/old/"]}},
              "_prev_admin_config": {"operator": "older"}}
    db = FakeDb(before)
    original = await agent._apply_discovery_to_db(
        1, {"allow_url_patterns": ["/new/"]}, db, expected_config=before,
    )
    assert original == before
    assert json.loads(db.writes[0]["expected"]) == before
    assert db.commits == 1


@pytest.mark.asyncio
async def test_newer_config_protected_on_discovery_apply_and_rollback():
    db = FakeDb({"operator": "new"})
    with pytest.raises(RuntimeError, match="changed during"):
        await agent._apply_discovery_to_db(1, {"allow_url_patterns": []}, db, expected_config={})
    assert not db.writes and not db.commits
    db = FakeDb({"operator": "new"}, rowcount=0)
    with pytest.raises(RuntimeError, match="newer config"):
        await agent._restore_db_config(1, {}, db, expected_current={"repair": "old"})
    assert not db.commits
    assert json.loads(db.writes[0]["expected"]) == {"repair": "old"}


def test_newer_yaml_protected_on_apply_and_rollback(tmp_path):
    path = tmp_path / "university.yaml"
    path.write_text("operator: new\n")
    with pytest.raises(RuntimeError, match="changed during"):
        agent._apply_to_yaml(path, tmp_path, 1, SEED, {"discovery": {}}, expected_text="old\n")
    with pytest.raises(RuntimeError, match="newer config"):
        agent._restore_yaml(path, "original\n", expected_text="repair\n")
    assert path.read_text() == "operator: new\n"