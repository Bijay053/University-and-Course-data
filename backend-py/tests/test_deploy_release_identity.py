"""Regression tests for the production release-identity deployment contract."""

from pathlib import Path
from argparse import Namespace
import asyncio

import pytest

from deploy.safe_restart_smoke import _main


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
    )

    asyncio.run(_main(args))

    assert capsys.readouterr().out == (
        f"release identity passed: release={release} "
        "uni-api-py_match_elapsed_s=0.125 "
        "uni-celery_match_elapsed_s=1.750\n"
    )