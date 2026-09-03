"""Regression tests for the production release-identity deployment contract."""

from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = BACKEND_ROOT / "deploy"
GENERAL_ENV = "/root/University-and-Course-data/backend-py/.env"
RELEASE_ENV = "/root/University-and-Course-data/backend-py/.release.env"
OPENAI_ENV = "-/etc/university-portal/openai.env"


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
    assert _environment_files(service_name) == [GENERAL_ENV, OPENAI_ENV, RELEASE_ENV]


def test_openai_client_is_a_declared_runtime_dependency() -> None:
    requirements = (BACKEND_ROOT / "requirements.txt").read_text(encoding="utf-8")
    pyproject = (BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert any(line.startswith("openai>=") for line in requirements.splitlines())
    assert '"openai>=' in pyproject


def test_release_identity_smoke_check_covers_fastapi_and_celery() -> None:
    readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")

    smoke_check = readme.split("## Release-identity smoke check", maxsplit=1)[1]
    assert "for unit in uni-api-py uni-celery" in smoke_check
    assert 'journalctl -u "$unit"' in smoke_check
    assert 'grep -Fq "$expected"' in smoke_check
    assert "FastAPI and Celery reported" in smoke_check