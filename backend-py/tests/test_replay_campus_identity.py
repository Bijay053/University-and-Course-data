"""Shared page snapshots must not erase staged campus identities."""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest

from app.models.scraped_course import ScrapedCourse
from app.services.scraper.campus_fee_split import _apply_group, plan_campus_fees
from app.services.scraper.extractors.ulaw_fees import METHOD, parse_course_fees
from app.services.scraper.replay_extraction import _scoped_replay_values, _replay_job_inner


URL = "https://www.law.ac.uk/study/postgraduate/business/msc-healthcare-management/"


def parent_payload():
    html = """<h1>MSc Healthcare Management</h1>
    <a role="tab" href="#fees">International Students</a>
    <div id="fees"><table>
    <tr><td>2026/27 Course Fees</td><td></td></tr>
    <tr><td>London</td><td>£19,050</td></tr>
    <tr><td>Outside London</td><td>£17,500</td></tr>
    </table></div>"""
    authority = parse_course_fees(html, URL, today=date(2026, 9, 25))
    return {
        "course_name": "MSc Healthcare Management",
        "course_location": "London, Manchester",
        "course_website": URL,
        "international_fee": authority["international_fee"],
        "fee_year": authority["fee_year"],
        "fee_term": authority["fee_term"],
        "currency": "GBP",
        "extraction_method": {"international_fee": METHOD, "fee_variants": authority},
    }


def siblings():
    payload = parent_payload()
    groups, reason = plan_campus_fees(payload)
    assert reason is None
    rows = []
    for index, group in enumerate(groups, 1):
        row = ScrapedCourse(id=index, university_id=7, scrape_job_id="job", **deepcopy(payload))
        _apply_group(row, group, payload["course_name"], payload["course_location"])
        rows.append(row)
    return rows


def test_replay_payload_preserves_campus_ids_names_fees_and_official_url():
    for row in siblings():
        identity = (row.id, row.course_name, row.course_location, row.fee_scope_key, row.course_website)
        expected_fee = row.international_fee
        values = _scoped_replay_values(row, parent_payload())
        for key, value in values.items():
            setattr(row, key, value)
        assert row.international_fee == expected_fee
        assert (row.id, row.course_name, row.course_location, row.fee_scope_key, row.course_website) == identity


def test_replay_bad_mapping_clears_fee_and_requires_review():
    payload = parent_payload()
    payload["extraction_method"] = {}
    payload["international_fee"] = 9999
    for row in siblings():
        values = _scoped_replay_values(row, payload)
        assert values["international_fee"] is None
        assert values["auto_publish_status"] == "review"
        assert "campus_fee_scope_requires_review" in values["scrape_warnings"]
        assert "course_name" not in values
        assert "course_location" not in values


@pytest.mark.asyncio
async def test_replay_one_page_fans_out_to_locked_siblings(monkeypatch):
    import json
    from app.services.scraper import replay_extraction

    rows = siblings()
    snapshot = SimpleNamespace(
        course_url=URL, snapshot_type="json", storage_path="saved",
        original_extraction={}, fetched_at=None, scraper_commit=None,
        yaml_version=None, fetch_method="api",
    )

    class DB:
        committed = False
        statements = []

        async def execute(self, query, *args):
            self.statements.append(str(query))
            if len(self.statements) == 1:
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [snapshot]))
            if len(self.statements) == 2:
                return SimpleNamespace(fetchone=lambda: (7, URL, "ULaw", {}))
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

        async def commit(self):
            self.committed = True

    async def download(path):
        return json.dumps(parent_payload()).encode()

    monkeypatch.setattr(replay_extraction, "download_snapshot", download)
    db = DB()
    result = await _replay_job_inner("job", commit=True, max_courses=10, db=db)
    assert result["errors"] == 0
    assert db.committed
    assert "FOR UPDATE" in db.statements[2]
    assert [row.international_fee for row in rows] == [19050, 17500]
    assert [row.course_name for row in rows] == [
        "MSc Healthcare Management — London",
        "MSc Healthcare Management — Manchester",
    ]
    assert [row.id for row in rows] == [1, 2]
    assert all(row.course_website == URL for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_changed", [False, True])
async def test_ai_rescan_scopes_url_evidence_and_fences_source_changes(monkeypatch, source_changed):
    from app.services.scraper.ai_repair_agent import _run_extraction_scan
    from app.services.scraper.config import loader, context
    from app.services.scraper.pipelines import single_course

    rows = siblings()
    original_names = [row.course_name for row in rows]
    sample = [{"id": row.id, "course_website": URL} for row in rows]
    for row in rows:
        row.international_fee = None
        if source_changed:
            row.course_website = URL + "changed/"

    class DB:
        locks = []

        async def execute(self, query, *args):
            sql = str(query)
            if "COUNT(*)" in sql:
                return SimpleNamespace(scalar=lambda: 2)
            if "SELECT scrape_config" in sql:
                return SimpleNamespace(first=lambda: ({},))
            if "LIMIT" in sql:
                return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: sample))
            assert "FOR UPDATE" in sql
            row = rows[len(self.locks)]
            self.locks.append(row.id)
            return SimpleNamespace(scalar_one_or_none=lambda: row)

        async def commit(self):
            pass

    async def extract(**kwargs):
        return parent_payload()

    monkeypatch.setattr(loader, "load_uni_config", lambda **kwargs: None)
    monkeypatch.setattr(context, "set_uni_config", lambda value: None)
    monkeypatch.setattr(single_course, "extract_course", extract)
    db = DB()
    result = await _run_extraction_scan({
        "university_id": 7, "job_id": "job", "scrape_url": URL, "uni_name": "ULaw",
    }, {}, db)
    assert db.locks == [1, 2]
    assert [row.course_name for row in rows] == original_names
    if source_changed:
        assert all(row.international_fee is None for row in rows)
        assert len(result["errors"]) == 2
        assert all("source changed" in error["reason"] for error in result["errors"])
    else:
        assert [row.international_fee for row in rows] == [19050, 17500]