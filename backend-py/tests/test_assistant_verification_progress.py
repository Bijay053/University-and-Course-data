"""A child scrape's progress is not its repair acceptance verdict."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.routers.scrape_assistant import response, service
from app.models import ScrapeRuntimeJob
from app.services import ai_repair_workflow, review_counts


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_target", ["source", "verification-child"])
@pytest.mark.parametrize(
    "child_status,current,total,session_status,phase,expected_state",
    [
        ("queued", 0, 0, "running", "verification_queued", "running"),
        ("running", 7, 50, "running", "verifying", "running"),
        ("completed", 50, 50, "running", "verifying", "running"),
        ("completed", 50, 50, "completed", "needs_review", "error"),
        ("completed", 50, 50, "completed", "verified", "completed"),
        ("failed_degraded", 10, 50, "completed", "needs_review", "error"),
    ],
)
async def test_card_uses_current_child_counts_but_only_audited_acceptance_completes(
    monkeypatch, existing_target, child_status, current, total,
    session_status, phase, expected_state,
):
    source = SimpleNamespace(university_id=1, current=52, total_found=52, status="completed")
    child = SimpleNamespace(current=current, total_found=total, status=child_status)
    thread = SimpleNamespace(
        id=1, job_id="source", turn_id="turn", status="completed", phase="",
        error=None, activity=[], updated_at=service.now(),
        messages=[{
            "role": "assistant", "createdAt": service.now().isoformat(),
            "actions": [{
                "id": "repair", "kind": "repair_catalogue", "state": "running",
                "_sessionId": "session", "jobId": existing_target, "courseCount": 50,
            }],
        }],
    )
    original_source = deepcopy(vars(source))
    class Db:
        commits = 0
        async def get(self, model, key):
            assert model is ScrapeRuntimeJob
            return {"source": source, "verification-child": child}.get(key)
        async def commit(self):
            self.commits += 1
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("Status rendering must not update course records")
    db = Db()
    reconciler = AsyncMock(return_value={
        "status": session_status, "current_attempt": 1,
        "autonomous": {
            "phase": phase, "verification_job_id": "verification-child",
            "reason": "Stopped execution exhausted the verification time budget; partial sample only.",
        },
    })
    monkeypatch.setattr(ai_repair_workflow, "reconcile", reconciler)
    monkeypatch.setattr(review_counts, "pending_counts", AsyncMock(return_value={}))
    result = await response(thread, db)
    action = result["messages"][0]["actions"][0]
    assert action["jobId"] == "verification-child"
    assert action["progress"] == {
        "current": current, "total": total or 50, "status": child_status,
    }
    assert action["state"] == expected_state
    reconciler.assert_awaited_once_with("source", "session", db)
    if child_status == "failed_degraded":
        assert "time budget" in action["error"]
        assert "Verification finished" not in action["error"]
    assert "_sessionId" not in action
    assert vars(source) == original_source
    assert db.commits == 1
