from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services import snapshot_store
from app.services.snapshot_storage_monitor import next_health_state


class _Body:
    def __init__(self, value: bytes):
        self.value = value

    async def read(self) -> bytes:
        return self.value


class _FakeS3:
    def __init__(self, *, fail_get: Exception | None = None):
        self.value = b""
        self.fail_get = fail_get
        self.deleted = False

    async def put_object(self, **kwargs):
        self.value = kwargs["Body"]
        return {"VersionId": "canary-version"}

    async def get_object(self, **kwargs):
        if self.fail_get:
            raise self.fail_get
        return {"Body": _Body(self.value)}

    async def delete_object(self, **kwargs):
        assert kwargs["VersionId"] == "canary-version"
        self.deleted = True

    async def head_object(self, **kwargs):
        error = RuntimeError("missing")
        error.response = {
            "Error": {"Code": "NoSuchKey"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }
        raise error


class _ClientContext:
    def __init__(self, client):
        self.client_value = client

    async def __aenter__(self):
        return self.client_value

    async def __aexit__(self, *_args):
        return False


class _Session:
    def __init__(self, client):
        self.client_value = client

    def client(self, *_args, **_kwargs):
        return _ClientContext(self.client_value)


@pytest.mark.asyncio
async def test_canary_writes_reads_and_deletes(monkeypatch):
    client = _FakeS3()
    monkeypatch.setattr(snapshot_store, "is_enabled", lambda: True)
    monkeypatch.setattr(snapshot_store, "_bucket", lambda: "secret-bucket")
    monkeypatch.setattr(snapshot_store, "_make_async_session", lambda: _Session(client))

    result = await snapshot_store.run_storage_canary(timeout_seconds=1)

    assert result["ok"] is True
    assert client.deleted is True
    assert "secret-bucket" not in str(result)


@pytest.mark.asyncio
async def test_canary_sanitizes_failure_and_still_deletes(monkeypatch):
    error = RuntimeError("provider said secret-bucket access key ABC")
    client = _FakeS3(fail_get=error)
    monkeypatch.setattr(snapshot_store, "is_enabled", lambda: True)
    monkeypatch.setattr(snapshot_store, "_bucket", lambda: "secret-bucket")
    monkeypatch.setattr(snapshot_store, "_make_async_session", lambda: _Session(client))

    result = await snapshot_store.run_storage_canary(timeout_seconds=1)

    assert result == {
        "ok": False,
        "error_code": "provider_error",
        "message": "Snapshot storage could not complete the canary operation.",
    }
    assert client.deleted is True


def test_alert_starts_on_third_failure_and_respects_cooldown():
    now = datetime(2026, 9, 5, tzinfo=timezone.utc)
    second = next_health_state(
        previous_failures=1,
        previous_alert_sent_at=None,
        probe_ok=False,
        error_code="provider_error",
        checked_at=now,
    )
    third = next_health_state(
        previous_failures=2,
        previous_alert_sent_at=None,
        probe_ok=False,
        error_code="provider_error",
        checked_at=now,
    )
    within_cooldown = next_health_state(
        previous_failures=3,
        previous_alert_sent_at=now - timedelta(hours=23),
        probe_ok=False,
        error_code="provider_error",
        checked_at=now,
    )

    assert second["alert_active"] is False
    assert third["alert_active"] is True
    assert third["should_alert"] is True
    assert within_cooldown["alert_active"] is True
    assert within_cooldown["should_alert"] is False


def test_success_clears_failure_and_alert_state():
    state = next_health_state(
        previous_failures=9,
        previous_alert_sent_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
        probe_ok=True,
        error_code="",
        checked_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
    )
    assert state == {
        "failures": 0,
        "alert_active": False,
        "should_alert": False,
        "status": "healthy",
    }


def test_deliberately_disabled_storage_does_not_accumulate_alert_failures():
    state = next_health_state(
        previous_failures=9,
        previous_alert_sent_at=None,
        probe_ok=False,
        error_code="not_configured",
        checked_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
    )
    assert state == {
        "failures": 0,
        "alert_active": False,
        "should_alert": False,
        "status": "disabled",
    }


def test_monitor_uses_transaction_scoped_advisory_lock():
    source = Path("app/services/snapshot_storage_monitor.py").read_text()
    assert "pg_try_advisory_xact_lock" in source
    assert "pg_try_advisory_lock(" not in source
    assert "pg_advisory_unlock" not in source