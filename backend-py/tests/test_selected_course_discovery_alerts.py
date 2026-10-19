"""Offline regression tests executing the orchestrator's actual alert call sites.

Compile only the discovery/finalization statements so unrelated DB, Redis,
browser and paid extraction work cannot run. Alert persistence is a spy; the
existing delivery suite tests the persistence/notification implementation.
"""
from __future__ import annotations

import ast
import asyncio
import copy
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.scraper import orchestrator as orch
from app.services.scraper.stage_course import StageResult
# Reuse the offline staging fixture: real stage_course, fake database and
# deterministic verification/evidence hooks.
from tests.test_autonomous_verification_isolation import StageDB, stage_args, staging


URL = "https://www.northumbria.ac.uk/study-at-northumbria/courses/example/"


def _compile_block(statements, namespace):
    wrapper = ast.parse("async def run(): pass")
    wrapper.body[0].body = statements
    exec(compile(ast.fix_missing_locations(wrapper), "<alert-call-site>", "exec"), namespace)
    return namespace["run"]


@pytest.fixture
def alert_run(monkeypatch):
    alert = AsyncMock()
    monkeypatch.setattr(orch, "_persist_and_deliver_discovery_failure_alert", alert)
    tree = ast.parse(inspect.getsource(orch._run_claimed_scrape))
    discovery_gate = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.If) and "_TIER7_THRESHOLD" in ast.unparse(node.test)
    )
    final_calls = sorted(
        (
            node for node in ast.walk(tree)
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Await)
            and isinstance(node.value.value, ast.Call)
            and isinstance(node.value.value.func, ast.Name)
            and node.value.value.func.id == "_alert_failed_selected_course_recovery"
        ),
        key=lambda node: node.lineno,
    )
    assert len(final_calls) == 2  # ordinary completion and unexpected failure
    stage_calls = sorted(
        (
            node for node in ast.walk(tree)
            if isinstance(node, ast.If) and len(node.body) == 1
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Call)
            and isinstance(node.body[0].value.func, ast.Name)
            and node.body[0].value.func.id == "_record_selected_staging_outcome"
        ),
        key=lambda node: node.lineno,
    )
    assert len(stage_calls) == 2  # ordinary staging and recovery sweep
    outer_try = next(
        node for node in tree.body[0].body
        if isinstance(node, ast.Try) and node.finalbody
    )
    failure_handler = next(
        handler for handler in outer_try.handlers
        if isinstance(handler.type, ast.Name) and handler.type.id == "Exception"
    )

    def build(payload, *, candidates=1, staged=1, errors=0, fetch_failed=0,
              status="completed"):
        job = SimpleNamespace(
            runtime_job_id="report-child", university_id=42,
            url=URL,
            request_payload=copy.deepcopy(payload), fast_mode=False, status=status,
            imported=staged, total_found=candidates, current=candidates,
            skipped=0, errors=errors, discovered_config={"recovery_audit": ["unchanged"]},
        )
        summary = dict(discovered=candidates, discovered_raw=candidates,
                       staged=staged, skipped=0, errors=errors, fetch_failed=fetch_failed)
        db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock(), refresh=AsyncMock())
        failures = {}
        namespace = {
            **vars(orch), "db": db, "job": job, "summary": summary,
            "links": [{"url": URL}] * candidates,
            "_targeted_retry": orch._is_targeted_retry_payload(payload),
            "_TIER7_THRESHOLD": 3, "uni_id": 42, "uni_name": "Northumbria",
            "scrape_url": "https://www.northumbria.ac.uk/courses/",
            "_uni_cfg": SimpleNamespace(slug="northumbria"),
            "runtime_job_id": job.runtime_job_id, "exc": RuntimeError("selected URL failed"),
            "_selected_staging_failures": failures,
        }

        async def stage_outcome(result, *, sweep=False, url=URL):
            await _compile_block(
                [stage_calls[int(sweep)]],
                {**namespace, "res": result, "r": {"url": url},
                 "_sw_res": result, "_sweep_url": url},
            )()

        return SimpleNamespace(
            job=job, summary=summary, db=db, alert=alert,
            stage_outcome=stage_outcome, failures=failures,
            discover=_compile_block([discovery_gate], dict(namespace)),
            finish=_compile_block([final_calls[0]], dict(namespace)),
            fail=_compile_block(failure_handler.body, dict(namespace)),
        )

    return build


def report_payload(key="course_urls"):
    return {
        key: [URL],
        "retrySourceJobId": "parent-review",
        "courseReport": {"id": "report", "source_job_id": "parent-review"},
    }


@pytest.mark.parametrize("key", ["course_urls", "courseUrls"])
async def test_successful_one_course_report_has_no_discovery_failure(alert_run, key):
    run = alert_run(report_payload(key))
    before = copy.deepcopy((vars(run.job), run.summary))
    await run.discover()
    await run.finish()
    run.alert.assert_not_awaited()
    assert (vars(run.job), run.summary) == before
    run.db.commit.assert_not_awaited()


@pytest.mark.parametrize(
    "status,staged,errors,fetch_failed",
    [
        ("failed", 0, 1, 0),              # extraction error
        ("failed_degraded", 0, 0, 1),     # selected URL fetch failed
        ("failed", 0, 0, 0),              # all selected URLs filtered out
        ("completed", 1, 1, 0),           # partial result must not hide errors
    ],
)
async def test_failed_selected_url_alerts_after_outcome(
    alert_run, status, staged, errors, fetch_failed,
):
    run = alert_run(report_payload(), status=status, staged=staged,
                    errors=errors, fetch_failed=fetch_failed)
    before = copy.deepcopy((vars(run.job), run.summary))
    await run.discover()
    run.alert.assert_not_awaited()  # a submitted URL is not proof it succeeded
    await run.finish()
    run.alert.assert_awaited_once()
    args = run.alert.await_args.kwargs
    assert args["job"] is run.job
    assert args["candidates_found"] == 1
    assert args["diagnostic"] == {
        "scope": "selected_course_recovery",
        "scrape_url": "https://www.northumbria.ac.uk/courses/",
        "selected_count": 1, "candidates_found": 1, "staged": staged,
        "errors": errors, "fetch_failed": fetch_failed, "status": status,
        "source_job_id": "parent-review",
    }
    assert (vars(run.job), run.summary) == before


@pytest.mark.parametrize("candidates", [0, 1, 2, 3])
@pytest.mark.parametrize("payload", [
    {},
    {"courseReport": {"source_job_id": "parent-review"}},
    {"retrySourceJobId": "parent-review", "courseUrls": []},
    {"courseUrls": ["not-a-url"]},
])
async def test_full_catalogue_retains_low_coverage_alert(alert_run, payload, candidates):
    run = alert_run(payload, candidates=candidates)
    # Fast/sample runs and a report label alone must not suppress discovery alerts.
    run.job.fast_mode = True
    await run.discover()
    await run.finish()
    assert run.alert.await_count == int(candidates < 3)
    if candidates < 3:
        assert run.alert.await_args.kwargs["candidates_found"] == candidates
        assert run.alert.await_args.kwargs["diagnostic"]["uni_slug"] == "northumbria"


async def test_unexpected_selected_url_exception_still_alerts(alert_run):
    run = alert_run(report_payload(), status="running", staged=0)
    await run.discover()
    result = await run.fail()
    assert result["ok"] is False
    assert run.job.status == "failed"
    assert run.job.error_message == "selected URL failed"
    run.alert.assert_awaited_once()
    assert run.alert.await_args.kwargs["diagnostic"]["status"] == "failed"


async def test_stopped_selected_recovery_is_not_a_failure_alert(alert_run):
    run = alert_run(report_payload(), status="stopped", staged=0, errors=1)
    await run.discover()
    await run.finish()
    run.alert.assert_not_awaited()


async def test_alert_delivery_failure_does_not_change_recovery_outcome(alert_run):
    run = alert_run(report_payload(), status="failed", staged=0, errors=1)
    run.alert.side_effect = RuntimeError("notification unavailable")
    before = copy.deepcopy((vars(run.job), run.summary))
    await run.finish()
    run.alert.assert_awaited_once()
    assert (vars(run.job), run.summary) == before


async def test_multi_url_recovery_failure_is_not_hidden_by_catalogue_threshold(alert_run):
    payload = report_payload()
    payload["course_urls"] = [f"https://example.edu/courses/{i}" for i in range(4)]
    run = alert_run(payload, candidates=4, staged=3, fetch_failed=1,
                    status="completed_with_warnings")
    await run.discover()
    await run.finish()
    run.alert.assert_awaited_once()
    assert run.alert.await_args.kwargs["diagnostic"]["selected_count"] == 4


@pytest.mark.parametrize("sweep", [False, True])
@pytest.mark.parametrize("reason", [
    "flush failed: database unavailable",
    "evidence persistence failed: database unavailable",
    "commit failed: database unavailable",
    "course_name too short",
])
async def test_unsaved_selected_course_alerts_even_when_counted_as_skipped(
    alert_run, sweep, reason,
):
    run = alert_run(report_payload(), staged=0)
    run.summary["skipped"] = 1
    before = copy.deepcopy((vars(run.job), run.summary))
    await run.discover()
    await run.stage_outcome(StageResult(False, reason), sweep=sweep)
    await run.finish()
    run.alert.assert_awaited_once()
    diagnostic = run.alert.await_args.kwargs["diagnostic"]
    assert diagnostic["staging_failures"] == {orch.canonical_course_url_key(URL): reason}
    assert diagnostic["errors"] == diagnostic["fetch_failed"] == 0
    assert (vars(run.job), run.summary) == before


@pytest.mark.parametrize("result", [
    StageResult(True, "staged", scraped_course_id=123),
    StageResult(False, "rejected: duplicate_url_in_job"),
    StageResult(False, "rejected: online_only"),
    StageResult(False, "rejected: domestic_only"),
    StageResult(False, "rejected: blocked_page:category"),
    StageResult(False, "rejected: generic category page"),
])
async def test_resolved_or_deliberately_excluded_staging_is_not_an_alert(alert_run, result):
    run = alert_run(report_payload(), staged=0)
    await run.stage_outcome(StageResult(False, "commit failed: transient"))
    # A later successful stage, existing review row, or eligibility decision
    # resolves the staging failure; URL normalization avoids stale aliases.
    await run.stage_outcome(result, sweep=True, url=URL.rstrip("/"))
    await run.finish()
    assert run.failures == {}
    run.alert.assert_not_awaited()


async def test_real_stage_commit_failure_remains_alertable(alert_run, staging):
    stage_db = StageDB()
    stage_db.commit = AsyncMock(side_effect=RuntimeError("database commit unavailable"))
    stage_db.rollback = AsyncMock()
    args = stage_args()
    result = await staging.stage_course(stage_db, **args, preserve_existing=True)
    assert not result.saved
    assert result.reason == "commit failed: database commit unavailable"
    stage_db.commit.assert_awaited_once()
    stage_db.rollback.assert_awaited_once()

    payload = report_payload()
    payload["course_urls"] = [args["source_url"]]
    run = alert_run(payload, staged=0)
    run.summary["skipped"] = 1  # preserve the legacy orchestrator accounting
    await run.discover()
    await run.stage_outcome(result, url=args["source_url"])
    await run.finish()
    run.alert.assert_awaited_once()
    assert run.alert.await_args.kwargs["diagnostic"]["staging_failures"] == {
        orch.canonical_course_url_key(args["source_url"]): result.reason,
    }
    assert run.job.status == "completed"  # alert-only; no status/audit rewrite
    assert run.summary["errors"] == 0


@pytest.mark.parametrize("exhaustion", ["time", "cost"])
async def test_selected_recovery_budget_alerts_after_wrapper_finalization(
    alert_run, monkeypatch, exhaustion,
):
    from app.services.scraper import job_claim
    from app.services.scraper.autonomous_verification import VerificationLimits

    run = alert_run(report_payload(), status="running", staged=0)
    run.db.get = AsyncMock(return_value=run.job)
    run.db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: 0))
    run.job.cost_ceiling_hit = False
    monkeypatch.setattr(job_claim, "claim_runtime_job", AsyncMock(return_value=True))
    monkeypatch.setattr(orch, "prepare_full_catalogue_review", lambda job: False)
    monkeypatch.setattr(orch, "validate_verification", AsyncMock(
        # Selected course reports use the continuation round; round zero
        # intentionally removes selected URLs to force fresh discovery.
        return_value=VerificationLimits(
            "parent-review", "session", time_budget_seconds=0.01, round_index=1,
        ),
    ))

    async def exhaust(*args):
        if exhaustion == "cost":
            raise orch.VerificationBudgetExceeded("cost budget exhausted")
        await asyncio.Event().wait()

    monkeypatch.setattr(orch, "_run_claimed_scrape", exhaust)
    result = await orch._run_scrape_entry(run.db, run.job.runtime_job_id)
    assert result["ok"] is False
    assert result["reason"] == (
        "time_budget_exhausted" if exhaustion == "time" else "gemini_primary_budget_exhausted"
    )
    run.alert.assert_awaited_once()
    assert run.alert.await_args.kwargs["diagnostic"]["status"] == "failed_degraded"
    assert run.job.status == "failed_degraded"
    assert run.job.imported == 0
    assert run.job.discovered_config["recovery_audit"] == ["unchanged"]


def test_selected_failure_notification_does_not_claim_catalogue_discovery_failed(monkeypatch):
    from app.services.scraper import alert_delivery
    from unittest.mock import Mock

    send = Mock(return_value={"success": True, "detail": "ok"})
    monkeypatch.setattr(alert_delivery, "_notifications_enabled", lambda: True)
    monkeypatch.setattr(alert_delivery, "SLACK_WEBHOOK_URL", "https://example.test/notify")
    monkeypatch.setattr(alert_delivery, "ALERT_EMAIL_TO", None)
    monkeypatch.setattr(alert_delivery, "_send_slack_raw", send)
    result = alert_delivery.deliver_discovery_failure_alert(
        uni_name="Northumbria", uni_id=42, scrape_url=URL, candidates_found=1,
        diagnostic={"scope": "selected_course_recovery", "selected_count": 1,
                    "staged": 0, "errors": 1, "status": "failed"},
    )
    assert result["status"] == "delivered"
    _, subject, body = send.call_args.args
    assert "Selected-course recovery failed" in subject
    assert "All discovery tiers" not in body
    assert "threshold < 3" not in body
    assert "errors: 1" in body
