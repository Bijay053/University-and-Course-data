"""Focused contracts for the production-safe restart smoke command."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from deploy.safe_restart_smoke import (
    DEFAULT_COURSE_URL,
    DEFAULT_EXPECTED_SKIP_REASON,
    DEFAULT_UNIVERSITY_ID,
    SmokeFailure,
    resolve_expected_release,
    verify_service_release_identity,
    validate_database_rehearsal_requirement,
    validate_done_payload,
    validate_idle_counts,
)
from deploy.database_refresh_rehearsal_proof import write_rehearsal_proof
from app.services.scraper.orchestrator import _is_safe_restart_smoke_payload


@pytest.mark.parametrize("status", ["queued", "running", "awaiting_approval"])
def test_aborts_when_any_active_job_status_is_present(status: str) -> None:
    counts = {"queued": 0, "running": 0, "awaiting_approval": 0}
    counts[status] = 1

    with pytest.raises(SmokeFailure, match=status):
        validate_idle_counts(counts)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {
            "totalFound": 0,
            "imported": 0,
            "skipped": 0,
            "skip_reasons": {},
        },
        {
            "totalFound": 1,
            "imported": 1,
            "skipped": 0,
            "skip_reasons": {},
        },
    ],
)
def test_zero_work_or_non_skip_sample_fails(payload: dict | None) -> None:
    with pytest.raises(SmokeFailure):
        validate_done_payload(payload)


def test_accepts_exact_canonical_default_policy_skip_payload() -> None:
    assert DEFAULT_EXPECTED_SKIP_REASON == "domestic_only"
    validate_done_payload(
        {
            "totalFound": 1,
            "imported": 0,
            "skipped": 1,
            "skip_reasons": {"domestic_only": 1},
        }
    )


def test_rejects_staged_rows_errors_or_extra_skip_reasons() -> None:
    clean = {
        "totalFound": 1,
        "imported": 0,
        "skipped": 1,
        "errors": 0,
        "skip_reasons": {"domestic_only": 1},
    }
    with pytest.raises(SmokeFailure, match="staged_rows=1"):
        validate_done_payload(clean, staged_rows=1)
    with pytest.raises(SmokeFailure, match="errors=1"):
        validate_done_payload({**clean, "errors": 1})
    with pytest.raises(SmokeFailure, match="canonical"):
        validate_done_payload(
            {**clean, "skip_reasons": {"domestic_only": 1, "parser_error": 1}}
        )


def test_checked_in_sample_resolves_university_by_hostname() -> None:
    assert DEFAULT_UNIVERSITY_ID is None
    assert DEFAULT_COURSE_URL == (
        "https://www.torrens.edu.au/courses/business/"
        "bachelor-of-applied-business-marketing-partnership-with-ducere"
    )


def test_managed_database_environment_loads_before_app_imports() -> None:
    source = Path("deploy/safe_restart_smoke.py").read_text()
    assert source.index("_load_managed_database_environment()") < source.index(
        "from app.database import AsyncSessionLocal"
    )


def test_smoke_marker_requires_exactly_one_valid_target_url() -> None:
    assert _is_safe_restart_smoke_payload(
        {"safeRestartSmoke": True, "courseUrls": [DEFAULT_COURSE_URL]}
    )
    assert not _is_safe_restart_smoke_payload(
        {
            "safeRestartSmoke": True,
            "courseUrls": [DEFAULT_COURSE_URL, "https://example.edu/course/two"],
        }
    )
    assert not _is_safe_restart_smoke_payload(
        {"safeRestartSmoke": True, "courseUrls": []}
    )


def test_release_file_preserves_full_sha_used_by_running_services(tmp_path) -> None:
    full_sha = "a" * 40
    release_file = tmp_path / ".release.env"
    release_file.write_text(f"RELEASE_REVISION={full_sha}\n", encoding="utf-8")

    assert resolve_expected_release(release_file, fallback="a" * 12) == full_sha


def test_metadata_only_package_uses_release_file_without_git(tmp_path) -> None:
    release_file = tmp_path / ".release.env"
    release_file.write_text("RELEASE_REVISION=build-2026-09-07\n", encoding="utf-8")

    assert (
        resolve_expected_release(release_file, fallback="unknown")
        == "build-2026-09-07"
    )


def test_release_identity_retries_until_delayed_exact_journal_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journals = iter(
        ["worker booting", "Celery worker starting (release_revision=" + "a" * 40 + ")"]
    )
    sleeps: list[float] = []

    def fake_run(command: list[str], **_kwargs) -> str:
        if command[:2] == ["systemctl", "show"]:
            return "123"
        if command[0] == "journalctl":
            return next(journals)
        return ""

    monkeypatch.setattr("deploy.safe_restart_smoke._run", fake_run)
    monkeypatch.setattr(
        "deploy.safe_restart_smoke._read_process_environment",
        lambda _pid: [b"RELEASE_REVISION=" + b"a" * 40],
    )
    times = iter([10.0, 11.0, 12.25])
    monkeypatch.setattr(
        "deploy.safe_restart_smoke.time.monotonic", lambda: next(times)
    )
    monkeypatch.setattr(
        "deploy.safe_restart_smoke.time.sleep", lambda seconds: sleeps.append(seconds)
    )

    elapsed = verify_service_release_identity(
        "uni-api-py",
        "a" * 40,
        journal_since="2026-09-11T00:00:00+00:00",
        timeout_seconds=15,
    )
    assert sleeps == [1.0]
    assert elapsed == 2.25


def test_release_identity_reports_immediate_match_elapsed_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **_kwargs) -> str:
        if command[:2] == ["systemctl", "show"]:
            return "123"
        if command[0] == "journalctl":
            return "Python backend starting up (release_revision=" + "a" * 40 + ")"
        return ""

    times = iter([20.0, 20.125])
    monkeypatch.setattr("deploy.safe_restart_smoke._run", fake_run)
    monkeypatch.setattr(
        "deploy.safe_restart_smoke._read_process_environment",
        lambda _pid: [b"RELEASE_REVISION=" + b"a" * 40],
    )
    monkeypatch.setattr(
        "deploy.safe_restart_smoke.time.monotonic", lambda: next(times)
    )

    elapsed = verify_service_release_identity(
        "uni-api-py",
        "a" * 40,
        journal_since="2026-09-11T00:00:00+00:00",
    )

    assert elapsed == 0.125


def test_release_identity_rejects_process_environment_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "deploy.safe_restart_smoke._run",
        lambda command, **_kwargs: "123" if command[:2] == ["systemctl", "show"] else "",
    )
    monkeypatch.setattr(
        "deploy.safe_restart_smoke._read_process_environment",
        lambda _pid: [b"RELEASE_REVISION=" + b"b" * 40],
    )

    with pytest.raises(SmokeFailure, match="does not have RELEASE_REVISION"):
        verify_service_release_identity(
            "uni-api-py",
            "a" * 40,
            journal_since="2026-09-11T00:00:00+00:00",
        )


@pytest.mark.parametrize(
    "journal",
    [
        "",
        "Celery worker starting (release_revision=" + "b" * 40 + ")",
        "Python backend starting up (debug=False, release_revision=" + "a" * 40 + "0)",
    ],
)
def test_release_identity_rejects_missing_or_mismatched_journal_line(
    journal: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(command: list[str], **_kwargs) -> str:
        if command[:2] == ["systemctl", "show"]:
            return "123"
        if command[0] == "journalctl":
            return journal
        return ""

    times = iter([0.0, 1.0])
    monkeypatch.setattr("deploy.safe_restart_smoke._run", fake_run)
    monkeypatch.setattr(
        "deploy.safe_restart_smoke._read_process_environment",
        lambda _pid: [b"RELEASE_REVISION=" + b"a" * 40],
    )
    monkeypatch.setattr(
        "deploy.safe_restart_smoke.time.monotonic", lambda: next(times)
    )

    with pytest.raises(SmokeFailure, match="did not report exact"):
        verify_service_release_identity(
            "uni-api-py",
            "a" * 40,
            journal_since="2026-09-11T00:00:00+00:00",
            timeout_seconds=0,
        )


@pytest.mark.parametrize(
    "journal",
    [
        "Python backend starting up (debug=False, release_revision=" + "a" * 40 + ")",
        "Celery worker starting (release_revision=" + "a" * 40 + ")",
    ],
)
def test_release_identity_accepts_real_startup_message_forms(
    journal: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(command: list[str], **_kwargs) -> str:
        if command[:2] == ["systemctl", "show"]:
            return "123"
        if command[0] == "journalctl":
            return journal
        return ""

    monkeypatch.setattr("deploy.safe_restart_smoke._run", fake_run)
    monkeypatch.setattr(
        "deploy.safe_restart_smoke._read_process_environment",
        lambda _pid: [b"RELEASE_REVISION=" + b"a" * 40],
    )
    verify_service_release_identity(
        "uni-api-py",
        "a" * 40,
        journal_since="2026-09-11T00:00:00+00:00",
        timeout_seconds=0,
    )


def test_rejects_legacy_online_only_reason_key() -> None:
    with pytest.raises(SmokeFailure, match="canonical"):
        validate_done_payload(
            {
                "totalFound": 1,
                "imported": 0,
                "skipped": 1,
                "skip_reasons": {"rejected:_online_only": 1},
            }
        )


class _Kms:
    def __init__(self, private_key) -> None:
        self.private_key = private_key

    def describe_key(self, **_kwargs):
        return {
            "KeyMetadata": {
                "Arn": "arn:aws:kms:ap-south-1:123456789012:key/test-key",
                "KeyUsage": "SIGN_VERIFY",
                "KeySpec": "RSA_2048",
                "Enabled": True,
            }
        }

    def sign(self, *, Message, **_kwargs):
        return {
            "Signature": self.private_key.sign(
                Message,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )
        }


class _SigningSession:
    def __init__(self, private_key) -> None:
        self.kms = _Kms(private_key)

    def client(self, name, region_name):
        assert name == "kms"
        assert region_name == "ap-south-1"
        return self.kms


def _write_proof(
    tmp_path: Path, *, completed_at: datetime
) -> tuple[Path, Path]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    signers = tmp_path / "signers.json"
    signers.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "signers": [
                    {
                        "account_id": "123456789012",
                        "signing_key_arn": (
                            "arn:aws:kms:ap-south-1:123456789012:key/test-key"
                        ),
                        "public_key_pem": public_key_pem,
                    }
                ],
            }
        )
    )
    proof = tmp_path / "proof.json"
    template = Path("deploy/database-secret-refresh-rehearsal.yaml")
    write_rehearsal_proof(
        proof,
        session=_SigningSession(private_key),
        signing_key_id="alias/database-refresh-rehearsal-proof",
        account_id="123456789012",
        region="ap-south-1",
        run_id="a" * 16,
        template_path=template,
        completed_at=completed_at,
    )
    return proof, signers


def test_accepts_recent_matching_database_rehearsal_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(timezone.utc)
    proof, signers = _write_proof(tmp_path, completed_at=now)
    monkeypatch.setattr(
        "deploy.safe_restart_smoke.REHEARSAL_TEMPLATE",
        Path("deploy/database-secret-refresh-rehearsal.yaml"),
    )
    monkeypatch.setattr(
        "deploy.safe_restart_smoke.REHEARSAL_SIGNERS", signers
    )
    validate_database_rehearsal_requirement(
        proof,
        expected_account_id="123456789012",
        max_age_hours=24,
    )


def test_rejects_stale_or_wrong_account_database_rehearsal_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proof, signers = _write_proof(
        tmp_path,
        completed_at=datetime.now(timezone.utc) - timedelta(hours=25),
    )
    monkeypatch.setattr(
        "deploy.safe_restart_smoke.REHEARSAL_TEMPLATE",
        Path("deploy/database-secret-refresh-rehearsal.yaml"),
    )
    monkeypatch.setattr("deploy.safe_restart_smoke.REHEARSAL_SIGNERS", signers)
    with pytest.raises(SmokeFailure, match="stale"):
        validate_database_rehearsal_requirement(
            proof,
            expected_account_id="123456789012",
            max_age_hours=24,
        )
    with pytest.raises(SmokeFailure, match="account or template"):
        validate_database_rehearsal_requirement(
            proof,
            expected_account_id="210987654321",
            max_age_hours=24,
        )


def test_requires_database_rehearsal_account_before_maintenance(
    tmp_path: Path,
) -> None:
    with pytest.raises(SmokeFailure, match="ACCOUNT_ID"):
        validate_database_rehearsal_requirement(
            tmp_path / "missing.json",
            expected_account_id=None,
            max_age_hours=24,
        )


def test_rejects_modified_or_production_attested_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proof, signers = _write_proof(
        tmp_path, completed_at=datetime.now(timezone.utc)
    )
    payload = json.loads(proof.read_text(encoding="utf-8"))
    payload["region"] = "us-east-1"
    proof.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        "deploy.safe_restart_smoke.REHEARSAL_TEMPLATE",
        Path("deploy/database-secret-refresh-rehearsal.yaml"),
    )
    monkeypatch.setattr("deploy.safe_restart_smoke.REHEARSAL_SIGNERS", signers)
    with pytest.raises(SmokeFailure, match="signature"):
        validate_database_rehearsal_requirement(
            proof,
            expected_account_id="123456789012",
            max_age_hours=24,
        )


def test_signed_proof_contains_no_aws_credentials_or_signed_url(
    tmp_path: Path,
) -> None:
    proof, _signers = _write_proof(
        tmp_path, completed_at=datetime.now(timezone.utc)
    )
    serialized = proof.read_text(encoding="utf-8")
    assert "X-Amz-Security-Token" not in serialized
    assert "X-Amz-Credential" not in serialized
    assert "test-session-token" not in serialized
    assert "test-signing-secret" not in serialized
    assert "aws_identity_attestation_url" not in serialized
    assert base64.b64decode(json.loads(serialized)["signature_base64"], validate=True)
    with pytest.raises(SmokeFailure, match="production"):
        validate_database_rehearsal_requirement(
            proof,
            expected_account_id="905043442097",
            max_age_hours=24,
        )