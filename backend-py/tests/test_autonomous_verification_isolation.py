"""Offline isolation/budget regressions: no database, Redis or paid requests."""
from __future__ import annotations

import asyncio
import ast
import importlib
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import datetime, timezone

import pytest

from app.services.scraper import orchestrator as orch
from app.services.scraper.autonomous_verification import (
    VerificationBudgetExceeded,
    VerificationLimits,
    cap_verification_links,
    persist_verification_metadata,
    run_bounded_verification,
    schedule_snapshot,
    validate_verification,
)


def family(**overrides):
    policy = dict(parent_job_id="parent", session_id="session", **overrides)
    child = SimpleNamespace(
        runtime_job_id="child", university_id=42, job_type="scrape",
        request_payload={"autonomousVerification": policy},
        discovered_config={}, fast_mode=True, status="running",
        cost_ceiling_hit=False, total_gemini_cost_usd=0,
    )
    workflow = {
        "job_id": "parent", "session_id": "session", "university_id": 42,
        "status": "running",
        "autonomous": {"verification_job_id": "child", "phase": "verification_queued"},
    }
    parent = SimpleNamespace(
        university_id=42, request_payload={"aiRepairWorkflow": workflow},
    )
    db = SimpleNamespace(
        get=AsyncMock(side_effect=lambda model, key, **kw: parent if key == "parent" else child),
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: 3)),
        commit=AsyncMock(), rollback=AsyncMock(),
    )
    return db, child, parent


async def test_limits_are_bounded_and_fresh_flags_cannot_be_overridden():
    db, child, _ = family(max_courses=5000, time_budget_seconds=10000, cost_cap_usd=99)
    child.request_payload.update(
        resumeCourseIds=[1], resumeSourceJobIds=["old"],
        courseUrls=["https://example.edu/course"], course_urls=["https://example.edu/course"],
        forceDiscovery=False, fastMode=True,
    )
    limits = await validate_verification(db, child)
    assert (limits.max_courses, limits.time_budget_seconds, limits.cost_cap_usd) == (50, 600, 2)
    persist_verification_metadata(child, limits)
    assert child.fast_mode is False
    assert child.request_payload["forceDiscovery"] is True
    assert child.request_payload["fastMode"] is False
    assert not {"resumeCourseIds", "resumeSourceJobIds", "courseUrls", "course_urls"} & child.request_payload.keys()
    assert child.discovered_config["autonomousVerification"]["full_catalogue_verified"] is False


async def test_fenced_continuation_keeps_only_explicit_urls_and_never_resume_ids():
    db, child, parent = family(round_index=1, cost_cap_usd=0.75)
    child.request_payload.update(
        courseUrls=["https://example.edu/course/2"],
        resumeCourseIds=[1], resumeSourceJobIds=["old"],
    )
    limits = await validate_verification(db, child)
    assert limits.round_index == 1
    assert limits.cost_cap_usd == 0.75
    persist_verification_metadata(child, limits)
    assert child.request_payload["courseUrls"] == ["https://example.edu/course/2"]
    assert "resumeCourseIds" not in child.request_payload
    assert "resumeSourceJobIds" not in child.request_payload


async def test_persistence_keeps_parent_bound_report_programme_proof():
    proof = {
        "https://example.edu/programme/foundation/": {
            "kind": "foundation",
            "title": "Foundation in Liberal Arts",
            "evidence": "official page title + programme and admissions/international copy",
        },
    }
    db, child, _ = family(round_index=1, verified_programmes=proof)
    limits = await validate_verification(db, child)

    persist_verification_metadata(child, limits, discovered_candidates=1)
    persist_verification_metadata(child, limits, staged_courses=1)

    assert child.request_payload["autonomousVerification"]["verified_programmes"] == proof
    assert child.discovered_config["autonomousVerification"]["verified_programmes"] == proof


@pytest.mark.parametrize("key,value", [
    ("max_courses", 0), ("max_courses", -1), ("max_courses", 1.5),
    ("max_courses", True), ("max_courses", "50"),
    ("time_budget_seconds", float("nan")), ("time_budget_seconds", float("inf")),
    ("time_budget_seconds", 0), ("cost_cap_usd", -2), ("cost_cap_usd", None),
    ("round_index", 2), ("round_index", -1), ("round_index", True),
])
async def test_invalid_limits_fail_closed(key, value):
    db, child, _ = family(**{key: value})
    with pytest.raises(ValueError):
        await validate_verification(db, child)


@pytest.mark.parametrize("mutation", [
    "missing_parent", "missing_workflow", "wrong_session", "wrong_child",
    "wrong_university", "stale", "wrong_type", "self_parent",
])
async def test_caller_cannot_forge_internal_identity(mutation):
    db, child, parent = family()
    workflow = parent.request_payload["aiRepairWorkflow"]
    if mutation == "missing_parent":
        db.get.side_effect = None
        db.get.return_value = None
    elif mutation == "missing_workflow":
        parent.request_payload = {}
    elif mutation == "wrong_session":
        workflow["session_id"] = "newer-session"
    elif mutation == "wrong_child":
        workflow["autonomous"]["verification_job_id"] = "different-child"
    elif mutation == "wrong_university":
        parent.university_id = 99
    elif mutation == "stale":
        workflow["status"] = "completed"
    elif mutation == "wrong_type":
        child.job_type = "repair"
    else:
        child.request_payload["autonomousVerification"]["parent_job_id"] = "child"
    with pytest.raises(ValueError):
        await validate_verification(db, child)


async def test_normal_jobs_have_no_verification_policy():
    db, child, _ = family()
    child.request_payload = {}
    assert await validate_verification(db, child) is None
    db.get.assert_not_awaited()


@pytest.mark.parametrize("count,cap,expected,capped", [
    (120, 9999, 50, True), (50, 9999, 50, False), (10, 9999, 10, False),
    (40, 20, 20, True),
])
def test_final_cap_survives_yaml_overrides_and_records_scope(count, cap, expected, capped):
    _, child, _ = family()
    limits = VerificationLimits("parent", "session")
    links = [{"url": f"https://example.edu/course/{i}"} for i in range(count)]
    result = cap_verification_links(child, limits, links, cap)
    assert result == links[:expected]
    metadata = child.discovered_config["autonomousVerification"]
    assert metadata["discovered_candidates"] == count
    assert metadata["selected_courses"] == expected
    assert metadata["capped"] is capped
    assert metadata["coverage_measured"] is True
    assert metadata["full_catalogue_verified"] is False
    assert child.discovered_config["pipeline_stats"]["verification_capped"] is capped
    assert child.request_payload["autonomousVerification"] == metadata
    assert metadata["total_cost_cap_enforced"] is False
    assert metadata["cost_scope"] == "observed_course_gemini_primary_only"
    assert metadata["selected_urls"] == [
        f"https://example.edu/course/{i}" for i in range(expected)
    ]


def test_final_cap_canonical_deduplicates_before_selected_cardinality():
    _, child, _ = family()
    limits = VerificationLimits("parent", "session")
    links = [
        {"url": "https://www.example.edu/course/a/?utm_source=x"},
        {"url": "https://example.edu/course/a"},
        {"url": "https://example.edu/course/b"},
    ]
    result = cap_verification_links(child, limits, links, 50)
    assert result == [links[0], links[2]]
    metadata = child.discovered_config["autonomousVerification"]
    assert metadata["selected_courses"] == 2
    assert metadata["discovered_candidates"] == 2
    assert metadata["raw_discovered_candidates"] == 3
    assert metadata["duplicate_candidates_removed"] == 1
    assert metadata["capped"] is False


async def test_timeout_cancels_work_and_snapshots_before_returning_degraded():
    db, child, _ = family()
    limits = VerificationLimits("parent", "session", time_budget_seconds=0.01)
    cleanup = []

    async def snapshot():
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.append("snapshot")

    async def pipeline():
        schedule_snapshot(snapshot())
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.append("pipeline")

    result = await run_bounded_verification(db, child, limits, pipeline)
    assert set(cleanup) == {"pipeline", "snapshot"}
    assert result["ok"] is False
    assert result["reason"] == "time_budget_exhausted"
    assert child.status == "failed_degraded"
    assert child.imported == 3
    assert child.completed_at
    assert "partial review sample" in child.error_message
    assert child.discovered_config["autonomousVerification"]["budget_exhausted"] == "time_budget_exhausted"
    assert child.discovered_config["autonomousVerification"]["capped"] is None
    assert child.discovered_config["autonomousVerification"]["coverage_measured"] is False
    db.rollback.assert_awaited_once()


async def test_cost_ceiling_is_explicit_failure_not_success():
    db, child, _ = family()

    async def pipeline():
        raise VerificationBudgetExceeded("Gemini-primary ceiling reached; transport excluded")

    result = await run_bounded_verification(
        db, child, VerificationLimits("parent", "session"), pipeline,
    )
    assert result["ok"] is False
    assert result["reason"] == "gemini_primary_budget_exhausted"
    assert child.status == "failed_degraded"
    assert child.cost_ceiling_hit is True
    assert "transport excluded" in child.error_message


async def test_external_cancellation_is_not_relabelled_success():
    db, child, _ = family()

    async def pipeline():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await run_bounded_verification(
            db, child, VerificationLimits("parent", "session"), pipeline,
        )
    assert child.status == "running"


@pytest.mark.parametrize("fenced", [False, True])
async def test_real_orchestrator_timeout_skips_cleanup_and_releases_tasks_and_locks(monkeypatch, fenced):
    """Exercise the actual pipeline's early setup/finally under cancellation."""
    import redis.asyncio as aioredis
    from app.services.scraper import job_claim

    db, child, _ = family(time_budget_seconds=0.01)
    async def blocked_query(statement, *a, **kw):
        if "count(" in str(statement):
            return SimpleNamespace(scalar_one=lambda: 0)
        await asyncio.Event().wait()

    db.execute.side_effect = blocked_query
    monkeypatch.setattr(job_claim, "claim_runtime_job", AsyncMock(return_value=True))
    clear = AsyncMock(side_effect=AssertionError("must not delete prior review rows"))
    monkeypatch.setattr(orch, "_clear_stale_dedup", clear)
    monkeypatch.setattr(orch.settings, "max_concurrent_scrapes", 0)
    redis = SimpleNamespace(
        set=AsyncMock(return_value=True), get=AsyncMock(return_value="child"),
        delete=AsyncMock(), eval=AsyncMock(return_value=1), aclose=AsyncMock(),
    )
    monkeypatch.setattr(aioredis, "from_url", lambda *a, **kw: redis)
    closed = []

    async def background(*a):
        try:
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    monkeypatch.setattr(orch, "_heartbeat_pulser", background)
    monkeypatch.setattr(orch, "_stop_poller", background)

    class EmitSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, *args):
            pass

        async def commit(self):
            pass

    monkeypatch.setattr(orch, "AsyncSessionLocal", EmitSession)
    from contextlib import nullcontext
    from app.services import worker_fencing
    from app.services.scraper import fenced_redis_locks
    acquire_lock = AsyncMock(return_value=True)
    release_lock = AsyncMock(return_value=True)
    monkeypatch.setattr(fenced_redis_locks, "acquire_university_lock", acquire_lock)
    monkeypatch.setattr(fenced_redis_locks, "release_university_lock", release_lock)
    owner = worker_fencing.Ownership("verification:child", "replacement-generation")
    with worker_fencing.ownership_scope(owner) if fenced else nullcontext():
        result = await orch._run_scrape_entry(db, "child")
    assert result["reason"] == "time_budget_exhausted"
    assert child.status == "failed_degraded"
    clear.assert_not_awaited()
    if fenced:
        acquire_lock.assert_awaited_once_with(db, redis, "scrape:uni_lock:42", "child")
        release_lock.assert_awaited_once_with(
            redis, "scrape:uni_lock:42", "child", "replacement-generation",
        )
        redis.set.assert_not_awaited()
    else:
        acquire_lock.assert_not_awaited()
        redis.eval.assert_awaited_once()
        assert redis.eval.await_args.args[2:4] == ("scrape:uni_lock:42", "scrape:uni_lock:42:generation")
    redis.aclose.assert_awaited_once()
    assert len(closed) == 2


async def test_production_verification_recovery_is_a_successful_targeted_noop(monkeypatch):
    """Durable verification rows must classify all-selected recovery as success."""
    import redis.asyncio as aioredis
    from app.services.scraper import pdf_link_discoverer

    selected = [
        "https://study.csu.edu.au/international/courses/bachelor-business",
        "https://study.csu.edu.au/international/courses/master-finance",
    ]
    recovered = [f"{selected[0]}/", f"{selected[1]}?utm_source=review"]
    university = SimpleNamespace(
        id=42,
        name="Charles Sturt University",
        country="Australia",
        scrape_url="https://study.csu.edu.au/international/courses",
        scrape_config=None,
    )

    class Result:
        def __init__(self, *, scalar=None, rows=()):
            self._scalar = scalar
            self._rows = list(rows)

        def scalar_one_or_none(self):
            return self._scalar

        def scalars(self):
            return SimpleNamespace(all=lambda: self._rows)

    class DB:
        def __init__(self):
            self.commit = AsyncMock()
            self.rollback = AsyncMock()

        async def execute(self, statement, *args, **kwargs):
            sql = str(statement)
            if "FROM universities" in sql:
                return Result(scalar=university)
            if "FROM scraped_courses" in sql:
                return Result(rows=recovered)
            raise AssertionError(f"unexpected production query: {sql}")

    class Job(SimpleNamespace):
        def __getattr__(self, name):
            return None

    job = Job(
        runtime_job_id="child",
        university_id=42,
        url=university.scrape_url,
        request_payload={
            "courseUrls": selected,
            "retrySourceJobId": "source",
            "courseReport": {"source_job_id": "source"},
        },
        discovered_config={},
        fast_mode=False,
        status="running",
    )
    db = DB()

    async def background(*args):
        await asyncio.Event().wait()

    class EmitSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, *args, **kwargs):
            pass

        async def commit(self):
            pass

    redis = SimpleNamespace(
        set=AsyncMock(return_value=True),
        eval=AsyncMock(return_value=1),
        delete=AsyncMock(),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(aioredis, "from_url", lambda *args, **kwargs: redis)
    monkeypatch.setattr(orch.settings, "max_concurrent_scrapes", 0)
    monkeypatch.setattr(orch, "_heartbeat_pulser", background)
    monkeypatch.setattr(orch, "_stop_poller", background)
    monkeypatch.setattr(orch, "AsyncSessionLocal", EmitSession)
    monkeypatch.setattr(
        orch, "_persist_and_deliver_discovery_failure_alert", AsyncMock()
    )
    monkeypatch.setattr(
        pdf_link_discoverer,
        "discover_pdf_links_for_university",
        AsyncMock(return_value=[]),
    )

    classification = {}

    class Classified(Exception):
        pass

    real_classifier = orch._targeted_retry_all_filtered_diagnostic

    def classify(**kwargs):
        classification.update(kwargs)
        classification["diagnostic"] = real_classifier(**kwargs)
        raise Classified

    monkeypatch.setattr(orch, "_targeted_retry_all_filtered_diagnostic", classify)

    limits = VerificationLimits("parent", "session", max_courses=50)
    result = await orch._run_claimed_scrape(db, job, limits)

    assert result["ok"] is False  # synthetic stop immediately after classification
    assert classification["extraction_urls"] == []
    assert set(classification["resolved_urls"]) == {
        orch.canonical_course_url_key(url) for url in selected
    }
    assert classification["diagnostic"] is None
    assert "targeted_retry_diagnostic" not in job.discovered_config


@pytest.fixture
def staging(monkeypatch):
    module = importlib.import_module("app.services.scraper.stage_course")
    monkeypatch.setattr(module, "infer_course_taxonomy", lambda *a, **kw: {})
    monkeypatch.setattr(module, "compute_completeness", lambda sc: SimpleNamespace(score=1.0))
    monkeypatch.setattr(module, "decide_eligibility", lambda *a: SimpleNamespace(status="eligible", reason=None))
    monkeypatch.setattr(module, "should_auto_publish", lambda sc: SimpleNamespace(auto_publish=True, score=100))
    monkeypatch.setattr(module, "_persist_evidence", AsyncMock(return_value=1))
    from app.services.review import conflicts
    from app.services.scraper import verification_engine, snapshot_save
    monkeypatch.setattr(conflicts, "detect_and_persist_conflicts", AsyncMock(return_value=0))
    monkeypatch.setattr(verification_engine, "run_field_verification",
                        AsyncMock(return_value={"avg_confidence": 100}))
    monkeypatch.setattr(snapshot_save, "persist_staged_row_backup", AsyncMock())
    return module


class StageDB:
    def __init__(self):
        self.existing = [
            SimpleNamespace(id=i, status=status, pte_overall=79, course_name="Bachelor of Business")
            for i, status in enumerate(("pending", "review_ready", "approved", "published"), 1)
        ]
        self.added = []
        self.deleted = False
        self.inheritance_read = False

    async def execute(self, statement):
        if statement.is_delete:
            self.deleted = True
            self.existing = [r for r in self.existing if r.status in {"approved", "published"}]
            return SimpleNamespace(rowcount=2)
        if len(statement.selected_columns) == 1:
            value = self.added[0].id if self.added else None
        else:
            self.inheritance_read = True
            value = next(r for r in self.existing if r.status == "approved")
        return SimpleNamespace(scalar_one_or_none=lambda: value)

    def add(self, row):
        row.id = 100 + len(self.added)
        self.added.append(row)

    async def flush(self):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass


def stage_args():
    url = "https://example.edu/courses/business"
    return {
        "scrape_job_id": "child", "university_id": 42,
        "course_name": "Bachelor of Business", "source_url": url,
        "payload": {"degree_level": "Bachelor's", "international_fee": 45000, "course_website": url},
        "evidence": [{
            "field_key": "international_fee", "value": 45000, "confidence": 1,
            "method": "fee:test", "source_url": url, "snippet": "International tuition: $45,000",
        }],
    }


async def test_stage_preserves_all_prior_statuses_without_inheriting_and_is_review_only(staging):
    db = StageDB()
    before = [vars(row).copy() for row in db.existing]
    args = stage_args()
    result = await staging.stage_course(db, **args, preserve_existing=True)
    assert result.saved, result.reason
    assert [vars(row) for row in db.existing] == before
    assert not db.deleted and not db.inheritance_read
    row = db.added[0]
    assert row.pte_overall is None  # approved row has 79; fresh absence remains visible
    assert row.status == "pending"
    assert row.auto_publish_status == "review"  # even with both scoring passes at 100%
    assert not any(e.get("method") == "approved_row:inherited" for e in args["evidence"])
    duplicate = await staging.stage_course(db, **stage_args(), preserve_existing=True)
    assert not duplicate.saved
    assert duplicate.reason == "rejected: duplicate_url_in_job"
    assert len(db.added) == 1


async def test_default_stage_retains_legacy_replacement_and_inheritance(staging):
    db = StageDB()
    result = await staging.stage_course(db, **stage_args())
    assert result.saved, result.reason
    assert db.deleted and db.inheritance_read
    assert db.added[0].pte_overall == 79
    assert db.added[0].auto_publish_status == "ready"


async def test_preserve_mode_does_not_bypass_global_filters(staging):
    db = StageDB()
    args = stage_args()
    args["source_url"] = "https://example.edu/news/business"
    result = await staging.stage_course(db, **args, preserve_existing=True)
    assert not result.saved
    assert "blocked_page" in result.reason
    assert not db.added and not db.deleted


def test_pipeline_guards_all_resume_and_post_run_mutation_paths():
    """Structural regression for late hooks too costly to execute in unit tests."""
    source = inspect.getsource(orch._run_claimed_scrape)
    tree = ast.parse(source)
    assert "_preserve_review = bool(_verification) or _full_review" in source
    assert "and not _preserve_review\n            and job.university_id" in source
    assert "if not _preserve_review and job.status == \"completed\" and _bypassed_resume_course_ids:" in source
    assert "if _sweep_links and not _preserve_review:" in source
    assert "_qi_row = None if _preserve_review else" in source
    assert "if _dq_critical_urls and not _preserve_review:" in source
    stage_calls = [
        node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "stage_course"
    ]
    assert len(stage_calls) == 2
    assert all(any(k.arg == "preserve_existing" and ast.unparse(k.value) == "_preserve_review"
                   for k in call.keywords) for call in stage_calls)
    early_return = source.index("# Stop before ALL mutation/dispatch hooks")
    assert early_return < source.index("from app.services.scraper.metrics import compute_run_metrics")
    assert early_return < source.index("run_quality_actions as")
    assert early_return < source.index("run_recovery_pass as")
    assert "_effective_batch_size = 1" in source
    assert "if not _cost_monitor.can_continue():" in source


@pytest.fixture
def generic_recovery(monkeypatch):
    from app.tasks import scrape_tasks as tasks

    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    rows = [
        SimpleNamespace(
            runtime_job_id=key, job_type="scrape", status=status,
            request_payload=payload, requeue_count=0, updated_at=old,
        )
        for key, status, payload in (
            ("ordinary", "queued", None),
            ("verification", "queued", {"autonomousVerification": {"session_id": "session"}}),
            ("malformed-marker", "queued", {"autonomousVerification": None}),
            ("live-child", "running", {"autonomousVerification": {}}),
        )
    ]

    class Session:
        def __init__(self):
            self.statements = []
            self.commit = AsyncMock()
            self.get = AsyncMock(return_value=rows[1])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, statement, *args):
            self.statements.append(statement)
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    session = Session()
    monkeypatch.setattr(tasks, "AsyncSessionLocal", lambda: session)
    return tasks, session, rows, old


@pytest.mark.parametrize("finder", ["_async_find_all_queued", "_async_find_stale"])
async def test_generic_queued_queries_exclude_workflow_children_without_mutation(generic_recovery, finder):
    from sqlalchemy.dialects import postgresql

    tasks, session, rows, old = generic_recovery
    result = await getattr(tasks, finder)()
    assert result == [("ordinary", "scrape", 0)]
    for child in rows[1:]:
        assert child.updated_at == old
        assert child.requeue_count == 0
    assert rows[-1].status == "running"  # never reclaim a live child
    sql = str(session.statements[0].compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
    ))
    assert "request_payload IS NULL" in sql
    assert "OR NOT (" in sql
    assert "scrape_runtime_jobs.request_payload ? 'autonomousVerification'" in sql


@pytest.mark.parametrize("hook", ["immediate", "stale"])
def test_generic_requeue_hooks_only_dispatch_ordinary_jobs(generic_recovery, monkeypatch, hook):
    tasks, session, rows, old = generic_recovery
    dispatched = []
    locks = []
    monkeypatch.setattr(tasks, "_sync_dispose", lambda: None)
    monkeypatch.setattr(tasks, "_run_db_coro", asyncio.run)
    monkeypatch.setattr(tasks, "_async_requeue_abandoned_bulk_fixes", AsyncMock(return_value=[]))
    increment = AsyncMock()
    monkeypatch.setattr(tasks, "_async_increment_requeue", increment)
    redis = SimpleNamespace(
        set=lambda key, *a, **kw: locks.append(key) or True,
        delete=lambda *a: None,
    )
    monkeypatch.setattr(tasks, "_get_redis", lambda: redis)
    monkeypatch.setattr(tasks.scrape_university, "delay", dispatched.append)
    if hook == "immediate":
        tasks._immediate_requeue_hook()
    else:
        assert tasks.requeue_stale_queued.run()["requeued"] == ["ordinary"]
        increment.assert_awaited_once_with("ordinary")
    assert dispatched == ["ordinary"]
    assert locks == ["scrape:requeue_lock:ordinary"]
    assert all(row.updated_at == old for row in rows[1:])


async def test_generic_max_requeue_cannot_terminalize_workflow_child(generic_recovery):
    tasks, session, rows, _ = generic_recovery
    await tasks._async_mark_failed_max_requeue("verification")
    assert rows[1].status == "queued"
    session.commit.assert_not_awaited()


async def test_unfenced_worker_failure_cannot_terminalize_verification_child(generic_recovery):
    tasks, session, rows, _ = generic_recovery
    rows[1].status = "running"
    await tasks._mark_failed("verification", "Worker process exited")
    assert rows[1].status == "running"
    session.commit.assert_not_awaited()


async def test_duplicate_child_delivery_retains_atomic_claim_fence(monkeypatch):
    from app.services.scraper import job_claim

    db, _, _ = family()
    monkeypatch.setattr(job_claim, "claim_runtime_job", AsyncMock(return_value=False))
    pipeline = AsyncMock()
    monkeypatch.setattr(orch, "_run_claimed_scrape", pipeline)
    result = await orch.run_scrape(db, "child")
    assert result == {"ok": False, "reason": "already_claimed"}
    pipeline.assert_not_awaited()
    db.get.assert_awaited_once()