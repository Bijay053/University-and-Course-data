import asyncio
from types import SimpleNamespace

from app.routers import scrape


class _Rows:
    def scalars(self):
        return self

    def all(self):
        return [SimpleNamespace(id=101, course_name="Fresh course")]


class _FakeDb:
    def __init__(self):
        self.statement = None

    async def get(self, _model, _job_id):
        return SimpleNamespace(
            runtime_job_id="job_current",
            university_id=42,
            request_payload=None,
            started_at=None,
            completed_at=None,
            total_found=1,
            imported=1,
            skipped=0,
            errors=0,
        )

    async def execute(self, statement):
        self.statement = statement
        return _Rows()


def test_job_review_does_not_include_older_pending_rows(monkeypatch):
    """A completed scrape's review request is strictly scoped to its job ID."""
    async def _no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        scrape,
        "_staged_row_to_dict",
        lambda row: {"id": row.id, "courseName": row.course_name},
    )
    monkeypatch.setattr(scrape, "_attach_evidence_bulk", _no_op)
    monkeypatch.setattr(scrape, "_attach_recovery_counts_bulk", _no_op)

    db = _FakeDb()
    response = asyncio.run(scrape.staged_one("job_current", db))

    query = str(db.statement)
    where_clause = query.split("WHERE", maxsplit=1)[1]
    assert "scraped_courses.scrape_job_id" in where_clause
    assert "scraped_courses.university_id" not in where_clause
    assert response["courses"] == [{"id": 101, "courseName": "Fresh course"}]


def test_job_review_includes_explicit_unresolved_continuation_chain(monkeypatch):
    """Review the original and all continuation batches as one pending set."""

    class _ChainRows:
        def scalars(self):
            return self

        def all(self):
            return [
                SimpleNamespace(id=201, course_name="Original course"),
                SimpleNamespace(id=202, course_name="Recovered course"),
            ]

    class _ChainDb:
        def __init__(self):
            self.statement = None
            self.jobs = {
                "job_retry_2": SimpleNamespace(
                    runtime_job_id="job_retry_2",
                    university_id=42,
                    request_payload={"retrySourceJobId": "job_retry_1"},
                    started_at=None,
                    completed_at=None,
                    total_found=20,
                    imported=20,
                    skipped=0,
                    errors=0,
                ),
                "job_retry_1": SimpleNamespace(
                    runtime_job_id="job_retry_1",
                    university_id=42,
                    request_payload={"retrySourceJobId": "job_original"},
                ),
                "job_original": SimpleNamespace(
                    runtime_job_id="job_original",
                    university_id=42,
                    request_payload=None,
                ),
            }

        async def get(self, _model, job_id):
            return self.jobs.get(job_id)

        async def execute(self, statement):
            self.statement = statement
            return _ChainRows()

    async def _no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        scrape,
        "_staged_row_to_dict",
        lambda row: {"id": row.id, "courseName": row.course_name},
    )
    monkeypatch.setattr(scrape, "_attach_evidence_bulk", _no_op)
    monkeypatch.setattr(scrape, "_attach_recovery_counts_bulk", _no_op)

    db = _ChainDb()
    response = asyncio.run(scrape.staged_one("job_retry_2", db))

    compiled = db.statement.compile()
    bound_values = {
        value
        for value in compiled.params.values()
        if isinstance(value, (list, tuple))
        for value in value
    }
    assert bound_values == {"job_retry_2", "job_retry_1", "job_original"}
    assert response["courses"] == [
        {"id": 201, "courseName": "Original course"},
        {"id": 202, "courseName": "Recovered course"},
    ]