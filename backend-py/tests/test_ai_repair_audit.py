"""Durability contracts for compact AI repair evidence."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.services.scraper.ai_repair_agent import (
    _audit_urls,
    attach_repair_snapshot_availability,
    fail_repair_audit,
    load_repair_audit,
    load_repair_audits,
    persist_repair_audit,
    run_ai_repair_loop,
)


class _Result:
    def __init__(self, *, rows=(), scalar=None):
        self._rows = rows
        self._scalar = scalar

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self


class _DB:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []
        self.committed = False

    async def execute(self, statement, params):
        self.calls.append((str(statement), params))
        return next(self.results)

    async def commit(self):
        self.committed = True


def _session():
    return {
        "session_id": "repair-1",
        "job_id": "job-1",
        "university_id": 7,
        "status": "completed",
        "attempts": [{
            "attempt_number": 1,
            "confidence": 91,
            "patches_proposed": [{"section": "recipe", "field": "fees.css", "new_value": ".fee"}],
            "patches_applied": [],
            "validation_errors": ["fees: changed a populated value"],
            "outcome": "rejected",
            "rollback_status": "unchanged",
            "extraction_validation": {
                "reports": [{
                    "field": "fees",
                    "rejection_reasons": ["changed a populated value"],
                    "samples": [{
                        "url": "https://example.edu/course/a",
                        "before": "$10,000",
                        "after": "$1,000",
                        "method": "css",
                        "preserved": False,
                    }],
                }],
            },
        }],
    }


def test_audit_urls_collects_sample_urls_without_html():
    assert _audit_urls(_session()) == ["https://example.edu/course/a"]
    assert "html" not in str(_session()).lower()


@pytest.mark.asyncio
async def test_persist_repair_audit_references_existing_snapshot():
    db = _DB([
        _Result(rows=[{"id": 42, "course_url": "https://example.edu/course/a", "snapshot_type": "html"}]),
        _Result(),
    ])
    await persist_repair_audit(_session(), db)

    assert db.committed
    evidence = db.calls[1][1]["evidence"]
    assert '"snapshot_id": 42' in evidence
    assert '"before": "$10,000"' in evidence
    assert '"after": "$1,000"' in evidence
    assert "<html" not in evidence.lower()


@pytest.mark.asyncio
async def test_load_repair_audit_returns_latest_evidence():
    evidence = {"job_id": "job-1", "status": "completed", "attempts": [{"outcome": "accepted"}]}
    db = _DB([_Result(scalar=evidence)])
    assert await load_repair_audit("job-1", db) == evidence


@pytest.mark.asyncio
async def test_load_repair_audits_returns_every_run_in_time_order():
    first = {"session_id": "repair-1", "started_at": "2026-09-05T00:00:00+00:00"}
    second = {"session_id": "repair-2", "started_at": "2026-09-05T01:00:00+00:00"}
    db = _DB([_Result(rows=[first, second])])

    assert await load_repair_audits("job-1", db) == [first, second]
    statement, params = db.calls[0]
    assert "ORDER BY created_at ASC, session_id ASC" in statement
    assert params == {"job_id": "job-1"}


@pytest.mark.asyncio
async def test_snapshot_availability_is_attached_without_changing_compact_audit(monkeypatch):
    import app.services.snapshot_store as snapshot_store

    async def availability(path, **_kwargs):
        return {
            "availability": "available" if path == "available-key" else "expired",
            "available": path == "available-key",
            "expires_at": "2026-09-01T00:00:00+00:00",
        }

    monkeypatch.setattr(snapshot_store, "snapshot_availability", availability)
    original = {
        "session_id": "repair-1",
        "snapshot_refs": [
            {"snapshot_id": 1, "url": "https://example.edu/a", "type": "html"},
            {"snapshot_id": 2, "url": "https://example.edu/b", "type": "repair"},
            {"snapshot_id": 3, "url": "https://example.edu/c", "type": "html"},
            {"snapshot_id": "invalid", "url": "https://example.edu/d", "type": "html"},
            "malformed-reference",
        ],
    }
    fetched_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    db = _DB([_Result(rows=[
        {
            "id": 1,
            "storage_path": "available-key",
            "snapshot_type": "html",
            "fetched_at": fetched_at,
        },
        {
            "id": 2,
            "storage_path": "expired-key",
            "snapshot_type": "repair",
            "fetched_at": fetched_at,
        },
    ])])

    result = await attach_repair_snapshot_availability([original], db)

    assert result[0]["snapshot_reference_counts"] == {
        "available": 1,
        "expired": 1,
        "missing": 1,
        "unavailable": 2,
    }
    assert [ref["availability"] for ref in result[0]["snapshot_refs"]] == [
        "available",
        "expired",
        "missing",
        "unavailable",
        "unavailable",
    ]
    assert "availability" not in original["snapshot_refs"][0]


@pytest.mark.asyncio
async def test_snapshot_metadata_failure_never_breaks_compact_audit():
    class _FailingDB:
        async def execute(self, *_args, **_kwargs):
            raise RuntimeError("page_snapshots temporarily unavailable")

    original = {
        "session_id": "repair-1",
        "attempts": [{"outcome": "accepted", "before": "old", "after": "new"}],
        "snapshot_refs": [
            {"snapshot_id": 42, "url": "https://example.edu/a", "type": "html"},
            {"snapshot_id": "legacy-invalid", "url": "https://example.edu/b", "type": "html"},
        ],
    }

    result = await attach_repair_snapshot_availability([original], _FailingDB())

    assert result[0]["attempts"] == original["attempts"]
    assert result[0]["snapshot_reference_counts"]["unavailable"] == 2
    assert [ref["availability"] for ref in result[0]["snapshot_refs"]] == [
        "unavailable",
        "unavailable",
    ]


@pytest.mark.asyncio
async def test_one_availability_check_failure_only_marks_that_reference_unavailable(monkeypatch):
    import app.services.snapshot_store as snapshot_store

    async def availability(path, **_kwargs):
        if path == "broken-key":
            raise RuntimeError("provider timeout")
        return {
            "availability": "available",
            "available": True,
            "expires_at": None,
        }

    monkeypatch.setattr(snapshot_store, "snapshot_availability", availability)
    fetched_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    db = _DB([_Result(rows=[
        {"id": 1, "storage_path": "good-key", "snapshot_type": "html", "fetched_at": fetched_at},
        {"id": 2, "storage_path": "broken-key", "snapshot_type": "html", "fetched_at": fetched_at},
    ])])
    runs = [{
        "session_id": "repair-1",
        "snapshot_refs": [
            {"snapshot_id": 1, "url": "https://example.edu/a", "type": "html"},
            {"snapshot_id": 2, "url": "https://example.edu/b", "type": "html"},
        ],
    }]

    result = await attach_repair_snapshot_availability(runs, db)

    assert result[0]["snapshot_reference_counts"]["available"] == 1
    assert result[0]["snapshot_reference_counts"]["unavailable"] == 1


@pytest.mark.asyncio
async def test_worker_failure_updates_the_existing_durable_run():
    queued = {
        "session_id": "repair-1",
        "job_id": "job-1",
        "university_id": 7,
        "status": "queued",
        "attempts": [],
        "queued_at": "2026-09-05T00:00:00+00:00",
    }
    db = _DB([_Result(scalar=queued), _Result()])
    failed = await fail_repair_audit(
        "job-1", 7, "repair-1", "Worker lease expired.", db
    )
    assert failed["status"] == "failed"
    assert failed["rollback_status"] == "unchanged"
    assert failed["queued_at"] == queued["queued_at"]
    assert '"status": "failed"' in db.calls[1][1]["evidence"]


@pytest.mark.asyncio
async def test_invalid_ai_response_is_recorded_as_rejected(monkeypatch):
    import app.services.ai.openai_client as openai_client
    import app.services.scraper.ai_repair_agent as agent

    async def value(result):
        return result

    ctx = {
        "university_id": 7,
        "uni_name": "Example University",
        "drop_rate": 0,
        "quality": {"total_staged": 1},
    }
    monkeypatch.setattr(agent, "read_session", lambda _job: {"university_id": 7})
    monkeypatch.setattr(agent, "_write_session", lambda *_args: None)
    monkeypatch.setattr(agent, "_gather_context", lambda *_args: value(ctx))
    monkeypatch.setattr(agent, "_quality_snapshot", lambda *_args: value({"total_staged": 1}))
    monkeypatch.setattr(agent, "_build_user_message", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(agent, "persist_repair_audit", lambda *_args: value(None))
    monkeypatch.setattr(
        openai_client,
        "chat_json",
        lambda **_kwargs: value({
            "diagnosis": "fee selector is missing",
            "root_cause": "fees",
            "confidence": 92,
            "explanation": "Proposed an invalid action.",
            "patches": [
                {
                    "section": "recipe",
                    "field": f"fees.rule_{index}",
                    "action": "replace",
                    "value": ".fee",
                }
                for index in range(4)
            ],
        }),
    )

    session = await run_ai_repair_loop("job-1", object())
    attempt = session["attempts"][0]
    assert attempt["outcome"] == "rejected"
    assert attempt["confidence"] == 92
    assert attempt["patches_proposed"][0]["field"] == "fees.rule_0"
    assert attempt["patches_proposed"][0]["new_value"] == ".fee"
    assert "Invalid repair response" in attempt["validation_errors"][0]


def test_migration_and_status_endpoint_keep_audit_private():
    migration = Path("scripts/apply_migration_047.py").read_text(encoding="utf-8")
    router = Path("app/routers/scrape.py").read_text(encoding="utf-8")
    assert "REFERENCES page_snapshots" not in migration  # snapshots may be pruned independently
    assert "evidence JSONB NOT NULL" in migration
    status_block = router[router.index('async def get_ai_repair_status'):router.index('@router.post("/jobs/{job_id}/auto-repair-filter")')]
    assert 'require_permission("scraping.view")' in status_block
    assert "load_repair_audits" in status_block
    assert 'session["runs"]' in status_block
    assert "attach_repair_snapshot_availability" in status_block
    assert 'for durable_field in ("snapshot_refs", "snapshot_reference_counts")' in status_block
    snapshot_router = Path("app/routers/snapshots.py").read_text(encoding="utf-8")
    text_block = snapshot_router[
        snapshot_router.index("async def get_snapshot_text"):
        snapshot_router.index("return PlainTextResponse", snapshot_router.index("async def get_snapshot_text"))
    ]
    assert 'require_permission("scraping.view")' in text_block


def test_history_card_hydrates_durable_audit():
    source = Path("../artifacts/university-portal/src/components/scrape-job-card.tsx").read_text(encoding="utf-8")
    assert 'data.status !== "not_started"' in source
    assert "Loaded from the permanent repair audit." in source
    assert "Compare repair runs" in source
    assert "source page{counts.expired === 1" in source
    assert "Compact before/after repair evidence is still preserved." in source
    assert "/api/scrape/snapshot/text/${ref.snapshot_id}" in source


def test_worker_claim_failure_is_persisted_without_redis():
    source = Path("app/tasks/auto_repair_task.py").read_text(encoding="utf-8")
    claim_failure = source[source.index("if not claim_repair_session"):source.index("release_repair_lease", source.index("if not claim_repair_session"))]
    assert "_persist_ai_repair_failure" in claim_failure