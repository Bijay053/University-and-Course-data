"""Security and transaction contracts for snapshot-storage installation."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest


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


def test_lifecycle_restore_passes_transition_default_at_top_level():
    calls = []

    class _Client:
        def put_bucket_lifecycle_configuration(self, **kwargs):
            calls.append(kwargs)

    installer.restore_lifecycle_configuration(
        _Client(),
        "bucket",
        {
            "Rules": [{"ID": "existing"}],
            "TransitionDefaultMinimumObjectSize": "all_storage_classes_128K",
        },
    )

    assert calls == [{
        "Bucket": "bucket",
        "LifecycleConfiguration": {"Rules": [{"ID": "existing"}]},
        "TransitionDefaultMinimumObjectSize": "all_storage_classes_128K",
    }]
    assert "TransitionDefaultMinimumObjectSize" not in calls[0][
        "LifecycleConfiguration"
    ]


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

    async def cleanup(_store, key, **_kwargs):
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


@pytest.mark.asyncio
async def test_cleanup_removes_versions_and_delete_markers_for_exact_key():
    deleted = []
    calls = 0

    class _Missing(Exception):
        response = {
            "Error": {"Code": "NoSuchKey"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def list_object_versions(self, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {
                    "Versions": [
                        {"Key": "smoke", "VersionId": "v1"},
                        {"Key": "smoke-neighbour", "VersionId": "v2"},
                    ],
                    "DeleteMarkers": [{"Key": "smoke", "VersionId": "marker"}],
                }
            return {"Versions": [], "DeleteMarkers": []}

        async def delete_objects(self, **kwargs):
            deleted.extend(kwargs["Delete"]["Objects"])

        async def head_object(self, **_kwargs):
            raise _Missing()

    client = _Client()
    store = SimpleNamespace(
        _make_async_session=lambda: SimpleNamespace(
            client=lambda *_args, **_kwargs: client
        )
    )
    with patch.dict(
        installer.os.environ,
        {"AWS_S3_BUCKET_NAME": "bucket"},
        clear=True,
    ):
        await installer._delete_smoke_snapshot(store, "smoke")

    assert deleted == [
        {"Key": "smoke", "VersionId": "v1"},
        {"Key": "smoke", "VersionId": "marker"},
    ]


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
    assert "snapshot_store.setup_lifecycle_rules()" in script
    assert "list_object_versions" in script
    assert "DeleteMarkers" in script
    assert "EnvironmentFile=/etc/university-portal/snapshot-storage.env" in script
    assert "systemctl restart uni-api-py uni-celery" in script
    assert 'systemctl restart "$service"' in script
    assert 'systemctl stop "$service"' in script
    assert 'test "$current" = "$state"' in script
    assert 'grep -Fq "[SNAPSHOT] enabled"' in script
    assert "snapshot_round_trip" in script
    assert "from install_snapshot_storage_via_ssm import" not in script
    assert "sys.path.insert" not in script
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

    heredocs = re.findall(
        r"<<'([A-Z_]+)'\n(.*?)\n\1",
        script,
        flags=re.DOTALL,
    )
    assert heredocs
    for marker, source in heredocs:
        compile(source, f"<generated-{marker}>", "exec")


@pytest.mark.parametrize(
    ("fault_stage", "expected"),
    [
        ("restart", "false # forced restart failure"),
        ("upload-response", "fault_stage='upload-response'"),
        ("cleanup", "fault_stage='cleanup'"),
    ],
)
def test_generated_transaction_has_controlled_integration_failure_points(
    fault_stage,
    expected,
):
    script = installer._install_script(
        encrypted_b64="ciphertext",
        key_path="/tmp/key.pem",
        cert_path="/tmp/cert.pem",
        fault_stage=fault_stage,
    )

    assert expected in script
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