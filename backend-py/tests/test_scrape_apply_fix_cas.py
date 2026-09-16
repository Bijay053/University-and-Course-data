"""Endpoint-level CAS regressions for operator URL-filter fixes."""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from app.routers.scrape import apply_scrape_fix
from app.services.scraper.auto_repair_candidates import filter_config_fingerprint


class _Mappings:
    def __init__(self, row):
        self.row = row

    def first(self):
        return self.row


class _Result:
    def __init__(self, *, row=None, rowcount=0):
        self._row = row
        self.rowcount = rowcount

    def mappings(self):
        return _Mappings(self._row)


class _ApplyDb:
    def __init__(self, *, current_config: dict, update_rowcount: int = 1):
        self.current_config = current_config
        self.update_rowcount = update_rowcount
        self.commits = 0
        self.rollbacks = 0
        self.updated_params: dict | None = None

    async def execute(self, statement, params):
        sql = str(statement)
        if "FROM scrape_runtime_jobs" in sql:
            return _Result(row={"university_id": 7})
        if "SELECT" in sql and "FROM universities" in sql:
            return _Result(
                row={
                    "name": "Example University",
                    "scrape_url": "https://example.edu",
                    "scrape_config": self.current_config,
                }
            )
        if "UPDATE universities" in sql:
            self.updated_params = params
            return _Result(rowcount=self.update_rowcount)
        raise AssertionError(f"unexpected SQL in endpoint test: {sql}")

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class _Discovery:
    allow_url_patterns = [r"/old-course/"]
    must_contain = []
    block_url_patterns = []
    course_detail_url_patterns = []


class _EffectiveConfig:
    discovery = _Discovery()


@pytest.fixture
def filter_snapshot():
    return {
        "allow_url_patterns": [r"/old-course/"],
        "must_contain": [],
        "block_url_patterns": [],
        "course_detail_url_patterns": [],
    }


@pytest.mark.asyncio
async def test_apply_fix_updates_and_commits_with_matching_snapshot(
    monkeypatch, filter_snapshot
):
    from app.services.scraper.config import loader

    monkeypatch.setattr(
        loader,
        "get_config_for_host",
        lambda **_kwargs: _EffectiveConfig(),
    )
    db = _ApplyDb(
        current_config={
            "admin_config": {
                "discovery": {
                    "allow_url_patterns": [r"/old-course/"],
                }
            }
        }
    )

    result = await apply_scrape_fix(
        "job-1",
        {
            "config_patch": {"discovery": {"allow_url_patterns": []}},
            "expected_filter_config": filter_snapshot,
            "expected_filter_config_fingerprint": filter_config_fingerprint(
                filter_snapshot
            ),
        },
        db,
        {},
    )

    assert result["ok"] is True
    assert db.commits == 1
    assert db.rollbacks == 0
    assert db.updated_params is not None
    saved = json.loads(db.updated_params["cfg"])
    assert saved["admin_config"]["discovery"]["allow_url_patterns"] == []
    assert "expected_sc" in db.updated_params


@pytest.mark.asyncio
async def test_apply_fix_returns_409_when_effective_filter_config_is_stale(
    monkeypatch, filter_snapshot
):
    from app.services.scraper.config import loader

    class _ChangedDiscovery(_Discovery):
        allow_url_patterns = [r"/new-course/"]

    class _ChangedConfig:
        discovery = _ChangedDiscovery()

    monkeypatch.setattr(
        loader,
        "get_config_for_host",
        lambda **_kwargs: _ChangedConfig(),
    )
    db = _ApplyDb(
        current_config={
            "admin_config": {
                "discovery": {
                    "allow_url_patterns": [r"/new-course/"],
                }
            }
        }
    )

    with pytest.raises(HTTPException) as exc_info:
        await apply_scrape_fix(
            "job-1",
            {
                "config_patch": {"discovery": {"allow_url_patterns": []}},
                "expected_filter_config": filter_snapshot,
                "expected_filter_config_fingerprint": filter_config_fingerprint(
                    filter_snapshot
                ),
            },
            db,
            {},
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["error"] == "filter_config_changed"
    assert db.commits == 0
    assert db.updated_params is None