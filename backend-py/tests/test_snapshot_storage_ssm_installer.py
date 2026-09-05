"""Security and transaction contracts for snapshot-storage installation."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch


DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy"
sys.path.insert(0, str(DEPLOY_DIR))
SPEC = importlib.util.spec_from_file_location(
    "snapshot_storage_installer",
    DEPLOY_DIR / "install_snapshot_storage_via_ssm.py",
)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def test_payload_requires_every_snapshot_storage_value():
    with patch.dict(
        installer.os.environ,
        {
            "AWS_S3_BUCKET_NAME": "bucket",
            "AWS_S3_REGION": "ap-south-1",
            "AWS_ACCESS_KEY_ID": "access",
        },
        clear=True,
    ):
        try:
            installer._environment_payload()
        except SystemExit as exc:
            assert "AWS_SECRET_ACCESS_KEY" in str(exc)
        else:
            raise AssertionError("missing snapshot secret should fail closed")


def test_direct_python_service_cmdline_is_discovered_without_shebang_parsing():
    raw = (
        b"/opt/university-portal/backend-py/.venv/bin/python3\0"
        b"/opt/university-portal/backend-py/.venv/bin/gunicorn\0"
        b"app.main:app\0"
    )

    assert installer._python_from_cmdline(raw) == (
        "/opt/university-portal/backend-py/.venv/bin/python3"
    )


def test_plaintext_secrets_never_enter_ssm_commands_or_output(capsys):
    calls = []
    encrypted_payloads = []
    secrets = {
        "AWS_S3_BUCKET_NAME": "private-snapshot-bucket",
        "AWS_S3_REGION": "ap-south-1",
        "AWS_ACCESS_KEY_ID": "test-access-key",
        "AWS_SECRET_ACCESS_KEY": 'test-$()-`touch /tmp/nope`-\\\\-"secret',
    }

    def run_command(_ssm, _instance_id, _comment, commands):
        calls.append(commands)
        if len(calls) == 1:
            return "certificate"
        return "installed-and-roundtrip-verified"

    class _Session:
        def client(self, *_args, **_kwargs):
            return object()

    def encrypt(_certificate, payload):
        encrypted_payloads.append(payload)
        return "ciphertext"

    with (
        patch.object(installer, "_session", return_value=_Session()),
        patch.object(installer, "_run_command", side_effect=run_command),
        patch.object(installer.base64, "b64decode", return_value=b"certificate"),
        patch.object(installer, "_encrypt_with_certificate", side_effect=encrypt),
        patch.dict(installer.os.environ, secrets, clear=True),
    ):
        installer.install("i-production", "ap-south-1")

    serialized_commands = repr(calls)
    output = capsys.readouterr().out
    for value in secrets.values():
        assert value not in serialized_commands
        assert value not in output
    assert "ciphertext" in serialized_commands
    assert len(encrypted_payloads) == 1
    payload = encrypted_payloads[0].decode()
    assert "$()" in payload
    assert "`touch /tmp/nope`" in payload
    assert 'SNAPSHOT_ENABLED="true"' in payload


def test_ambiguous_upload_failure_still_cleans_deterministic_key(monkeypatch):
    cleaned = []

    async def upload_snapshot(*_args, **_kwargs):
        return None

    async def cleanup(_store, key):
        cleaned.append(key)

    store = SimpleNamespace(
        is_enabled=lambda: True,
        build_s3_key=lambda *_args, **_kwargs: "deterministic-smoke-key",
        upload_snapshot=upload_snapshot,
    )
    monkeypatch.setattr(installer, "_delete_smoke_snapshot", cleanup)

    try:
        installer.asyncio.run(installer.snapshot_round_trip(store))
    except AssertionError:
        pass
    else:
        raise AssertionError("ambiguous upload must fail the smoke test")

    assert cleaned == ["deterministic-smoke-key"]


def test_host_transaction_is_root_only_reversible_and_round_trip_verified():
    script = installer._install_script(
        encrypted_b64="ciphertext",
        key_path="/tmp/key.pem",
        cert_path="/tmp/cert.pem",
    )

    assert "umask 077" in script
    assert "chmod 600" in script
    assert "trap rollback_on_error EXIT" in script
    assert "restore_previous" in script
    assert "restore_lifecycle" in script
    assert "get_bucket_lifecycle_configuration" in script
    assert "put_bucket_lifecycle_configuration" in script
    assert '"TransitionDefaultMinimumObjectSize"' in script
    assert "lifecycle_failed=0" in script
    assert "restore_lifecycle || lifecycle_failed=1" in script
    assert "EnvironmentFile=/etc/university-portal/snapshot-storage.env" in script
    assert "systemctl restart uni-api-py uni-celery" in script
    assert 'grep -Fq "[SNAPSHOT] enabled"' in script
    assert "snapshot_round_trip" in script
    assert '. "$env_path"' not in script
    assert 'cd "$workdir"' in script
    assert 'PYTHONPATH=. "$python"' in script

    syntax = subprocess.run(
        ["bash", "-n"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr


def test_service_templates_load_snapshot_environment():
    for name in ("uni-api-py.service", "uni-celery.service"):
        source = (DEPLOY_DIR / name).read_text(encoding="utf-8")
        assert "EnvironmentFile=-/etc/university-portal/snapshot-storage.env" in source