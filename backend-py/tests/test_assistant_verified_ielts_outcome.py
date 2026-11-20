from types import SimpleNamespace

import pytest

from app.services.assistant_fix_outcome import fix_outcome, verified_ielts_ids


def plan():
    return {"_ids": [1, 2, 3], "fields": ["ielts_overall"]}


def finished():
    return {"total": 3, "results": [{
        "id": i, "ok": True, "outcome": "completed", "made_progress": True,
        "updated_fields": [], "all_updated_fields": ["requirement_status"],
        "progress_fields": ["ielts_overall", "ielts_reading", "ielts_writing",
                            "ielts_listening", "ielts_speaking"],
        "all_refreshed_evidence_fields": ["ielts_overall", "ielts_reading", "ielts_writing",
                                         "ielts_listening", "ielts_speaking"],
    } for i in [1, 2, 3]]}


def test_verified_equal_value_evidence_is_not_reported_as_no_progress():
    state, text, rows = fix_outcome(plan(), finished(), {1, 2, 3})
    assert state == "completed"
    assert "3 courses with requested IELTS evidence verified" in text
    assert "Score values were unchanged" in text
    assert "0 unresolved, 0 failed" in text
    assert all(r["status"] == "verified" for r in rows)
    assert "Finishing the job did not verify" not in text


def test_refreshed_evidence_without_verified_applicability_is_not_success():
    assert fix_outcome(plan(), finished())[0] == "needs_review"
    state, text, _ = fix_outcome(plan(), finished(), {1, 2})
    assert state == "needs_review"
    assert "1 unresolved" in text


@pytest.mark.parametrize("change", ["no_progress", "no_evidence", "failure", "wrong_field", "wrong_scope"])
def test_verification_cannot_excuse_missing_failed_or_unrelated_results(change):
    action, job = plan(), finished()
    if change == "no_progress":
        for r in job["results"]:
            r["made_progress"] = False
    elif change == "no_evidence":
        for r in job["results"]:
            r["all_refreshed_evidence_fields"] = ["intake_months"]
    elif change == "failure":
        job["results"][0].update(ok=False, outcome="failed")
    elif change == "wrong_field":
        action["fields"] = ["international_fee"]
    else:
        job["results"][0]["id"] = 99
    state, text, rows = fix_outcome(action, job, {1, 2, 3})
    assert state == "needs_review"
    if change == "failure":
        assert "1 failed" in text
        assert rows[0]["status"] != "verified"


class Database:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    async def execute(self, query):
        self.calls.append(query)
        return self

    def scalars(self):
        return self

    def all(self):
        return self.rows


@pytest.mark.asyncio
async def test_live_verification_is_scoped_to_unpublished_pending_course_ids(monkeypatch):
    from app.services.scraper import requirement_status
    monkeypatch.setattr(requirement_status, "effective_requirement_status",
                        lambda r: {"englishComponents": {"state": r.verification}})
    db = Database([SimpleNamespace(id=1, verification="verified"),
                   SimpleNamespace(id=2, verification="unverified"),
                   SimpleNamespace(id=3, verification="missing")])
    assert await verified_ielts_ids(db, plan(), finished(), 114) == {1}
    query = db.calls[0]
    assert query.compile().params["university_id_1"] == 114
    assert set(query.compile().params["id_1"]) == {1, 2, 3}
    assert "course_id IS NULL" in str(query)
    assert "status IN" in str(query)


@pytest.mark.asyncio
async def test_no_live_verification_query_for_a_job_without_source_progress():
    db, job = Database([]), finished()
    for r in job["results"]:
        r["made_progress"] = False
    assert await verified_ielts_ids(db, plan(), job, 114) == set()
    assert not db.calls
