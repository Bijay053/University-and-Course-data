from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app import release_info


def test_release_revision_prefers_deployment_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    release_info.get_release_revision.cache_clear()
    monkeypatch.setenv("RELEASE_REVISION", "deploy-abc123")
    run = MagicMock(side_effect=AssertionError("git fallback should not run"))
    monkeypatch.setattr(release_info.subprocess, "run", run)

    assert release_info.get_release_revision() == "deploy-abc123"


def test_stale_sha_metadata_yields_checked_out_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_info.get_release_revision.cache_clear()
    monkeypatch.setenv(
        "RELEASE_REVISION",
        "3fa77558ca302f0a23042bc30914d28ef21e68df",
    )
    checked_out = "742b9d5306f609fe351567aecc282d01f1cf8e96"
    monkeypatch.setattr(
        release_info.subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"stdout": checked_out})(),
    )

    assert release_info.get_release_revision() == checked_out[:12]


def test_release_revision_is_explicit_when_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_info.get_release_revision.cache_clear()
    for name in release_info._REVISION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        release_info.subprocess,
        "run",
        MagicMock(side_effect=TimeoutError("git lookup stalled")),
    )

    assert release_info.get_release_revision() == release_info.UNKNOWN_RELEASE


def test_release_lookup_failure_is_cached_and_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_info.get_release_revision.cache_clear()
    for name in release_info._REVISION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    run = MagicMock(side_effect=OSError("git unavailable"))
    monkeypatch.setattr(release_info.subprocess, "run", run)

    assert release_info.get_release_revision() == release_info.UNKNOWN_RELEASE
    assert release_info.get_release_revision() == release_info.UNKNOWN_RELEASE
    run.assert_called_once()
