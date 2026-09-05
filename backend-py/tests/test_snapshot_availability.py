"""Lifecycle-aware availability checks for stored page snapshots."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import snapshots
from app.services import snapshot_store


class _MissingObject(Exception):
    def __init__(self):
        self.response = {
            "Error": {"Code": "NoSuchKey"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }


class _Client:
    def __init__(self, error=None):
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def head_object(self, **_kwargs):
        if self.error:
            raise self.error
        return {}


class _Session:
    def __init__(self, error=None):
        self.error = error

    def client(self, *_args, **_kwargs):
        return _Client(self.error)


@pytest.mark.asyncio
async def test_snapshot_availability_reports_available(monkeypatch):
    monkeypatch.setattr(snapshot_store, "is_enabled", lambda: True)
    monkeypatch.setattr(snapshot_store, "_bucket", lambda: "bucket")
    monkeypatch.setattr(snapshot_store, "_make_async_session", lambda: _Session())

    result = await snapshot_store.snapshot_availability(
        "universities/1/job/html/page.html.gz",
        snapshot_type="html",
        fetched_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )

    assert result["availability"] == "available"
    assert result["available"] is True
    assert result["expires_at"] == "2026-11-30T00:00:00+00:00"


@pytest.mark.asyncio
async def test_absent_object_is_expired_after_lifecycle_deadline(monkeypatch):
    monkeypatch.setattr(snapshot_store, "is_enabled", lambda: True)
    monkeypatch.setattr(snapshot_store, "_bucket", lambda: "bucket")
    monkeypatch.setattr(
        snapshot_store,
        "_make_async_session",
        lambda: _Session(_MissingObject()),
    )
    now = datetime(2026, 9, 5, tzinfo=timezone.utc)

    result = await snapshot_store.snapshot_availability(
        "universities/1/job/failed/page.html.gz",
        snapshot_type="failed",
        fetched_at=now - timedelta(days=31),
        now=now,
    )

    assert result["availability"] == "expired"
    assert result["available"] is False


@pytest.mark.asyncio
async def test_absent_object_is_missing_before_lifecycle_deadline(monkeypatch):
    monkeypatch.setattr(snapshot_store, "is_enabled", lambda: True)
    monkeypatch.setattr(snapshot_store, "_bucket", lambda: "bucket")
    monkeypatch.setattr(
        snapshot_store,
        "_make_async_session",
        lambda: _Session(_MissingObject()),
    )
    now = datetime(2026, 9, 5, tzinfo=timezone.utc)

    result = await snapshot_store.snapshot_availability(
        "universities/1/job/html/page.html.gz",
        snapshot_type="html",
        fetched_at=now - timedelta(days=3),
        now=now,
    )

    assert result["availability"] == "missing"


@pytest.mark.asyncio
async def test_disabled_storage_is_unavailable_not_missing(monkeypatch):
    monkeypatch.setattr(snapshot_store, "is_enabled", lambda: False)

    result = await snapshot_store.snapshot_availability(
        "universities/1/job/html/page.html.gz",
        snapshot_type="html",
        fetched_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )

    assert result["availability"] == "unavailable"
    assert result["available"] is False


@pytest.mark.asyncio
async def test_expired_text_source_returns_clear_gone_warning(monkeypatch):
    record = SimpleNamespace(
        snapshot_type="html",
        storage_path="expired-key",
        fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    class _DB:
        async def get(self, _model, _snapshot_id):
            return record

    async def expired(*_args, **_kwargs):
        return {
            "availability": "expired",
            "available": False,
            "expires_at": "2026-04-01T00:00:00+00:00",
        }

    monkeypatch.setattr(snapshots, "snapshot_availability", expired)

    with pytest.raises(HTTPException) as exc_info:
        await snapshots.get_snapshot_text(42, {}, _DB())

    assert exc_info.value.status_code == 410
    assert "compact AI repair audit remains available" in exc_info.value.detail