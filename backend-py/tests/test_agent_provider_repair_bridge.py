from types import SimpleNamespace
import pytest

from app.services.scrape_agent_v2.repair import diagnosis, proposal, execute, repair_proposals


def job(**updates):
    values = dict(runtime_job_id="test-job", status="completed", university_id=1,
                  discovered_config={}, gate_skip_counts={}, total_found=40, current=40,
                  imported=40, skipped=0, errors=0, release_revision="test")
    values.update(updates)
    return SimpleNamespace(**values)


def tools(prompt="fix this university scraper"):
    return SimpleNamespace(prompt=prompt, turn_id="turn", ctx={"university_id": 1},
                           service=SimpleNamespace(fingerprint=lambda value: "config-version"))


@pytest.mark.parametrize("status", ["queued", "running"])
def test_active_scrape_cannot_be_repaired(status):
    result = diagnosis(job(status=status), missing_fields=4)
    assert not result["repairAvailable"]
    assert "wait" in result["blockedReason"]


def test_missing_fields_offer_validated_repair():
    result = diagnosis(job(), missing_fields=4)
    assert result["repairAvailable"]
    assert result["counts"]["staged"] == 40
    assert result["changed"] is False


def test_low_staging_is_not_claimed_complete():
    result = diagnosis(job(imported=1), missing_fields=0)
    assert result["catalogueShortfall"]
    assert result["repairAvailable"]


def test_healthy_scrape_not_needlessly_repaired():
    assert not diagnosis(job(), missing_fields=0)["repairAvailable"]


@pytest.mark.parametrize("status", ["failed", "failed_degraded", "failed_provider", "error"])
def test_failed_empty_output_allows_bounded_discovery_diagnosis_without_inventing_a_cause(status):
    result=diagnosis(job(status=status,total_found=0,current=0,imported=0),missing_fields=0)
    assert result["repairAvailable"] and result["needsDiscoveryProbe"]
    assert not result["catalogueShortfall"] and not result.get("pipelineEvidence")


@pytest.mark.parametrize("status", ["queued", "running", "completed", "stopped"])
def test_empty_output_does_not_restart_active_healthy_or_stopped_jobs(status):
    result=diagnosis(job(status=status,total_found=0,current=0,imported=0),missing_fields=0)
    assert not result["repairAvailable"] and not result["needsDiscoveryProbe"]


def test_provider_pause_preserved():
    result = diagnosis(job(), missing_fields=4, imports_paused=True)
    assert not result["repairAvailable"]


@pytest.mark.parametrize("prompt", ["only explain the failure", "do not fix this"])
def test_read_only_request_cannot_propose_repair(prompt):
    result = proposal(tools(prompt), SimpleNamespace(scrape_config={}),
                      diagnosis(job(), missing_fields=1), "call")
    assert not result["ok"]
    assert not result["changed"]


def test_proposal_uses_existing_ui_confirmation_and_config_fence():
    report = diagnosis(job(), missing_fields=1)
    first = proposal(tools(), SimpleNamespace(scrape_config={}), report, "one")
    replay = proposal(tools(), SimpleNamespace(scrape_config={}), report, "two")
    action = first["proposal"]
    assert action == replay["proposal"]
    assert action["kind"] == "repair_catalogue"
    assert action["requiresConfirmation"] and action["courseCount"] > 10
    assert action["_configVersion"] == "config-version"
    assert action["state"] == "proposed" and not first["changed"]
    assert repair_proposals([first, replay]) == [action]


@pytest.mark.asyncio
async def test_provider_identity_is_checked_before_any_repair():
    class DB:
        async def get(self, model, identity):
            return job(university_id=2)
    with pytest.raises(ValueError, match="outside"):
        await execute(tools(), DB(), SimpleNamespace(job_id="test-job"),
                      "repair_scraper", "one")


@pytest.mark.asyncio
async def test_repair_status_reports_real_comparison_without_dispatch(monkeypatch):
    from app.services.scraper import ai_repair_agent
    comparison = {"sample_verified": False, "cumulative_staged_courses": 3,
                  "capped": True, "unresolved_fields": ["ielts_overall"]}
    async def load(*args):
        return {"status": "completed", "autonomous": {
            "phase": "needs_review", "comparison": comparison,
            "verification_job_id": "verification-child"}}
    monkeypatch.setattr(ai_repair_agent, "load_repair_audit", load)
    class DB:
        async def get(self, model, identity):
            from app.models import ScrapeRuntimeJob
            return job() if model is ScrapeRuntimeJob else SimpleNamespace()
    result = await execute(tools(), DB(), SimpleNamespace(job_id="test-job"),
                           "read_scraper_repair", "one")
    assert result["verification"] == comparison
    assert result["phase"] == "needs_review"
    assert result["verificationJobId"] == "verification-child"
    assert not result["changed"]


@pytest.mark.asyncio
async def test_agent_loop_can_diagnose_then_prepare_guarded_repair():
    import json
    from app.services.scrape_agent_v2.loop import run_loop
    observed = []
    class Tools:
        schemas = []
        async def execute(self, name, args, call_id):
            observed.append(name)
            report = diagnosis(job(), missing_fields=4)
            if name == "diagnose_scraper":
                return report
            return proposal(tools(), SimpleNamespace(scrape_config={}), report, call_id)
    def call(name):
        return {"role": "assistant", "tool_calls": [{
            "id": name, "type": "function",
            "function": {"name": name, "arguments": "{}"}}]}
    turns = iter([call("diagnose_scraper"), call("repair_scraper"),
                  {"role": "assistant", "content": "Repair prepared, awaiting approval."}])
    async def complete(history, schemas):
        return {"message": next(turns), "tokens": 1}
    async def checkpoint(*args):
        pass
    result = await run_loop([], Tools(), complete, checkpoint)
    receipts = [json.loads(m["content"]) for m in result["history"] if m["role"] == "tool"]
    assert observed == ["diagnose_scraper", "repair_scraper"]
    assert repair_proposals(receipts)[0]["requiresConfirmation"]
    assert all(not receipt["changed"] for receipt in receipts)
