"""Regression tests for the production release-identity deployment contract."""

from pathlib import Path
from argparse import Namespace
import asyncio

import pytest

from deploy.safe_restart_smoke import (
    SmokeFailure,
    _main,
    persist_deployment_timing_evidence,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = BACKEND_ROOT / "deploy"
GENERAL_ENV = "/opt/university-portal/backend-py/.env"
RELEASE_ENV = "/opt/university-portal/backend-py/.release.env"
OPENAI_ENV = "-/etc/university-portal/openai.env"
SNAPSHOT_ENV = "-/etc/university-portal/snapshot-storage.env"
DATABASE_ENV = "/etc/university-portal/database.env"


def _environment_files(service_name: str) -> list[str]:
    service_text = (DEPLOY_DIR / service_name).read_text(encoding="utf-8")
    return [
        line.removeprefix("EnvironmentFile=").strip()
        for line in service_text.splitlines()
        if line.startswith("EnvironmentFile=")
    ]


@pytest.mark.parametrize("service_name", ["uni-api-py.service", "uni-celery.service"])
def test_service_loads_authoritative_release_environment_last(
    service_name: str,
) -> None:
    assert _environment_files(service_name) == [
        GENERAL_ENV,
        OPENAI_ENV,
        RELEASE_ENV,
        SNAPSHOT_ENV,
        DATABASE_ENV,
    ]


def test_openai_client_is_a_declared_runtime_dependency() -> None:
    requirements = (BACKEND_ROOT / "requirements.txt").read_text(encoding="utf-8")
    pyproject = (BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert any(line.startswith("openai>=") for line in requirements.splitlines())
    assert '"openai>=' in pyproject


def test_release_identity_smoke_check_covers_fastapi_and_celery() -> None:
    readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")

    smoke_check = readme.split("## Release-identity smoke check", maxsplit=1)[1]
    assert "--release-identity-only" in smoke_check
    assert '--journal-since "$smoke_since"' in smoke_check
    assert "--release-identity-timeout-seconds 15" in smoke_check
    assert "exact full `RELEASE_REVISION`" in smoke_check


def test_release_identity_success_reports_only_sanitized_match_timings(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    release = "a" * 40
    monkeypatch.setattr(
        "deploy.safe_restart_smoke._verify_release_and_services",
        lambda **_kwargs: (
            release,
            {"uni-api-py": 0.125, "uni-celery": 1.75},
        ),
    )
    args = Namespace(
        release_identity_only=True,
        journal_since="2026-09-11T00:00:00+00:00",
        release_identity_timeout_seconds=15,
        deployment_evidence_path=tmp_path / "deployments.jsonl",
        recent_deployment_evidence=None,
    )

    asyncio.run(_main(args))

    output = capsys.readouterr()
    assert output.err == ""
    assert output.out == (
        f"release identity passed: release={release} "
        "uni-api-py_match_elapsed_s=0.125 "
        "uni-celery_match_elapsed_s=1.750\n"
    )
    record = __import__("json").loads(
        (tmp_path / "deployments.jsonl").read_text(encoding="utf-8")
    )
    assert record["revision"] == release
    assert record["api_match_elapsed_seconds"] == 0.125
    assert record["celery_match_elapsed_seconds"] == 1.75
    assert set(record) == {
        "revision",
        "timestamp",
        "api_match_elapsed_seconds",
        "celery_match_elapsed_seconds",
    }


def test_slow_release_identity_warns_and_still_persists_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    release = "c" * 40
    evidence_path = tmp_path / "deployments.jsonl"
    monkeypatch.setattr(
        "deploy.safe_restart_smoke._verify_release_and_services",
        lambda **_kwargs: (
            release,
            {"uni-api-py": 6.125, "uni-celery": 2.0},
        ),
    )

    asyncio.run(
        _main(
            Namespace(
                release_identity_only=True,
                journal_since="2026-09-11T12:29:00+00:00",
                release_identity_timeout_seconds=15,
                release_identity_warning_seconds=5,
                deployment_evidence_path=evidence_path,
                recent_deployment_evidence=None,
            )
        )
    )

    output = capsys.readouterr()
    assert output.err == "release identity warning: uni-api-py 6.125s\n"
    assert output.out.startswith("release identity passed:")
    assert evidence_path.exists()


def test_successful_identity_persists_only_sanitized_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime
    import json

    release = "b" * 40
    evidence_path = tmp_path / "deployments.jsonl"
    monkeypatch.setattr(
        "deploy.safe_restart_smoke._verify_release_and_services",
        lambda **_kwargs: (
            release,
            {"uni-api-py": 0.25, "uni-celery": 2.5},
        ),
    )
    monkeypatch.setattr(
        "deploy.safe_restart_smoke.persist_deployment_timing_evidence",
        lambda path, **kwargs: persist_deployment_timing_evidence(
            path,
            **kwargs,
            recorded_at=datetime(2026, 9, 11, 12, 30, tzinfo=UTC),
        ),
    )

    asyncio.run(
        _main(
            Namespace(
                release_identity_only=True,
                journal_since="2026-09-11T12:29:00+00:00",
                release_identity_timeout_seconds=15,
                deployment_evidence_path=evidence_path,
                recent_deployment_evidence=None,
            )
        )
    )

    assert json.loads(evidence_path.read_text(encoding="utf-8")) == {
        "revision": release,
        "timestamp": "2026-09-11T12:30:00Z",
        "api_match_elapsed_seconds": 0.25,
        "celery_match_elapsed_seconds": 2.5,
    }


def test_failed_identity_does_not_persist_success_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    evidence_path = tmp_path / "deployments.jsonl"

    def fail_identity(**_kwargs: object) -> object:
        raise SmokeFailure("release mismatch")

    monkeypatch.setattr(
        "deploy.safe_restart_smoke._verify_release_and_services", fail_identity
    )

    with pytest.raises(SmokeFailure, match="release mismatch"):
        asyncio.run(
            _main(
                Namespace(
                    release_identity_only=True,
                    journal_since="2026-09-11T12:29:00+00:00",
                    release_identity_timeout_seconds=15,
                    deployment_evidence_path=evidence_path,
                    recent_deployment_evidence=None,
                )
            )
        )

    assert not evidence_path.exists()


def test_recent_deployment_evidence_returns_newest_records(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime
    import json

    evidence_path = tmp_path / "deployments.jsonl"
    for hour in range(3):
        persist_deployment_timing_evidence(
            evidence_path,
            release=str(hour) * 40,
            api_match_elapsed_seconds=hour + 0.1,
            celery_match_elapsed_seconds=hour + 0.2,
            recorded_at=datetime(2026, 9, 11, hour, tzinfo=UTC),
        )

    asyncio.run(
        _main(
            Namespace(
                recent_deployment_evidence=2,
                deployment_evidence_path=evidence_path,
            )
        )
    )

    records = [
        json.loads(line) for line in capsys.readouterr().out.splitlines()
    ]
    assert [record["revision"] for record in records] == ["2" * 40, "1" * 40]