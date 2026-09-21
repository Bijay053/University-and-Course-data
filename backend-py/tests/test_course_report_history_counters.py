"""Historical child counters must use persisted jobs, not sample sizes."""
from types import SimpleNamespace

from app.routers.scrape_reports import report_result


def test_history_returns_real_processed_found_and_outcome_counts():
    def child(job_id, **counts):
        return SimpleNamespace(
            runtime_job_id=job_id, status="completed", completed_at=True,
            request_payload={"courseReport": {"id": "report", "kind": "missing"}},
            discovered_config={}, error_message=None, gate_skip_counts={},
            **counts,
        )

    prior = child("prior", current=17, total_found=50, imported=3, skipped=12, errors=2)
    latest = child("latest", current=4, total_found=33, imported=2, skipped=1, errors=1)
    result = report_result(latest, {"autonomous": {"phase": "needs_review"}}, [prior, latest])
    assert result["children"][0] == {
        "job_id": "prior", "status": "completed", "processed": 17,
        "found": 50, "staged": 3, "skipped": 12, "errors": 2, "verification": {},
    }
    assert result["children"][1]["processed"] == result["processed"] == 4
    assert result["children"][1]["found"] == result["found"] == 33