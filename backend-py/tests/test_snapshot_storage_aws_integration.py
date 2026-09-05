"""Disposable real-AWS proof for snapshot-storage lifecycle and cleanup.

Run only in a non-production AWS account:

    RUN_SNAPSHOT_STORAGE_AWS_INTEGRATION=1 \
      PYTHONPATH=. python -m pytest -m integration \
      tests/test_snapshot_storage_aws_integration.py -q
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

import pytest

from app.services import snapshot_store


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_SNAPSHOT_STORAGE_AWS_INTEGRATION") != "1",
        reason="requires an explicit disposable-AWS opt in",
    ),
]

DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy"
sys.path.insert(0, str(DEPLOY_DIR))
SPEC = importlib.util.spec_from_file_location(
    "snapshot_storage_installer_aws_integration",
    DEPLOY_DIR / "install_snapshot_storage_via_ssm.py",
)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)

_TRANSITION_DEFAULT = "varies_by_storage_class"
_UNRELATED_RULE = {
    "ID": "fixture-abort-incomplete-uploads",
    "Status": "Enabled",
    "Filter": {"Prefix": ""},
    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
}


def _client():
    import boto3

    expected_account = os.environ.get("SNAPSHOT_STORAGE_AWS_TEST_ACCOUNT_ID")
    if not expected_account:
        pytest.skip("requires SNAPSHOT_STORAGE_AWS_TEST_ACCOUNT_ID")
    session = boto3.Session(
        region_name=os.environ.get("AWS_S3_REGION", "us-east-1"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    )
    actual_account = session.client("sts").get_caller_identity()["Account"]
    if actual_account != expected_account:
        pytest.fail(
            "refusing disposable-AWS test in unexpected account "
            f"{actual_account!r}"
        )
    return session.client(
        "s3",
    )


def _all_versions(client, bucket: str, prefix: str = "") -> list[dict]:
    paginator = client.get_paginator("list_object_versions")
    return [
        item
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
        for item in (*page.get("Versions", []), *page.get("DeleteMarkers", []))
    ]


def _purge_bucket(client, bucket: str) -> None:
    versions = _all_versions(client, bucket)
    if versions:
        client.delete_objects(
            Bucket=bucket,
            Delete={
                "Objects": [
                    {"Key": item["Key"], "VersionId": item["VersionId"]}
                    for item in versions
                ],
                "Quiet": True,
            },
        )
    client.delete_bucket(Bucket=bucket)


@pytest.fixture(params=["unversioned", "versioned"])
def disposable_snapshot_bucket(request, monkeypatch):
    client = _client()
    region = os.environ.get("AWS_S3_REGION", "us-east-1")
    bucket = f"university-portal-snapshot-it-{uuid.uuid4().hex}"
    create = {"Bucket": bucket}
    if region != "us-east-1":
        create["CreateBucketConfiguration"] = {"LocationConstraint": region}
    client.create_bucket(**create)
    if request.param == "versioned":
        client.put_bucket_versioning(
            Bucket=bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )
    client.put_bucket_lifecycle_configuration(
        Bucket=bucket,
        LifecycleConfiguration={"Rules": [_UNRELATED_RULE]},
        TransitionDefaultMinimumObjectSize=_TRANSITION_DEFAULT,
    )
    monkeypatch.setenv("AWS_S3_BUCKET_NAME", bucket)
    monkeypatch.setenv("AWS_S3_REGION", region)
    monkeypatch.setenv("SNAPSHOT_ENABLED", "true")
    monkeypatch.delenv("AWS_S3_ENDPOINT_URL", raising=False)
    monkeypatch.setattr(snapshot_store, "_BUCKET", None)
    try:
        yield client, bucket
        residue = _all_versions(client, bucket, "universities/0/deployment-smoke-")
        assert residue == [], f"deployment smoke residue remained: {residue!r}"
    finally:
        snapshot_store._BUCKET = None
        _purge_bucket(client, bucket)


def _lifecycle(client, bucket: str) -> dict:
    response = client.get_bucket_lifecycle_configuration(Bucket=bucket)
    return {
        key: response[key]
        for key in ("Rules", "TransitionDefaultMinimumObjectSize")
        if key in response
    }


def _remote_host_state(ssm, instance_id: str) -> dict:
    output = installer._run_command(
        ssm,
        instance_id,
        "Inspect disposable snapshot-storage integration host",
        [
            """python3 - <<'PY'
import base64
import json
import os
import subprocess

paths = [
    "/etc/university-portal/snapshot-storage.env",
    "/etc/systemd/system/uni-api-py.service.d/99-snapshot-storage.conf",
    "/etc/systemd/system/uni-celery.service.d/99-snapshot-storage.conf",
]
state = {"files": {}, "services": {}}
for path in paths:
    try:
        state["files"][path] = base64.b64encode(open(path, "rb").read()).decode()
    except FileNotFoundError:
        state["files"][path] = None
for service in ("uni-api-py", "uni-celery"):
    result = subprocess.run(
        ["systemctl", "is-active", service],
        text=True,
        capture_output=True,
        check=False,
    )
    state["services"][service] = result.stdout.strip()
print(json.dumps(state, sort_keys=True, separators=(",", ":")))
PY"""
        ],
    )
    return json.loads(output)


def _validated_ssm_fixture(instance_id: str):
    region = os.environ.get("AWS_S3_REGION", "us-east-1")
    expected_account = os.environ["SNAPSHOT_STORAGE_AWS_TEST_ACCOUNT_ID"]
    session = installer._session()
    actual_account = session.client(
        "sts",
        region_name=region,
    ).get_caller_identity()["Account"]
    if actual_account != expected_account:
        pytest.fail(
            "refusing SSM integration in unexpected account "
            f"{actual_account!r}"
        )
    response = session.client("ec2", region_name=region).describe_instances(
        InstanceIds=[instance_id],
    )
    instances = [
        instance
        for reservation in response["Reservations"]
        for instance in reservation["Instances"]
    ]
    if len(instances) != 1:
        pytest.fail("disposable SSM fixture instance was not found uniquely")
    tags = {
        tag["Key"]: tag["Value"]
        for tag in instances[0].get("Tags", [])
    }
    if tags.get("SnapshotStorageIntegration") != "disposable":
        pytest.fail(
            "refusing SSM integration without "
            "SnapshotStorageIntegration=disposable"
        )
    return session


def _run_remote_transaction(
    instance_id: str,
    fault_stage: str,
    session,
) -> None:
    ssm = session.client(
        "ssm",
        region_name=os.environ.get("AWS_S3_REGION", "us-east-1"),
    )
    transfer_id = uuid.uuid4().hex
    remote_dir = "/root/.university-portal-secret-transfer"
    key_path = f"{remote_dir}/{transfer_id}.key.pem"
    cert_path = f"{remote_dir}/{transfer_id}.cert.pem"
    certificate_b64 = installer._run_command(
        ssm,
        instance_id,
        "Prepare disposable snapshot-storage integration transfer",
        [
            "set -eu",
            "umask 077",
            f"install -d -m 700 {remote_dir}",
            (
                "openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 1 "
                "-subj /CN=snapshot-storage-integration "
                f"-keyout {key_path} -out {cert_path} >/dev/null 2>&1"
            ),
            f"base64 -w0 {cert_path}",
        ],
    )
    certificate_pem = installer.base64.b64decode(certificate_b64).decode("ascii")
    encrypted_b64 = installer._encrypt_with_certificate(
        certificate_pem,
        installer._environment_payload(),
    )
    installer._run_command(
        ssm,
        instance_id,
        f"Force snapshot-storage {fault_stage} rollback",
        [
            installer._install_script(
                encrypted_b64=encrypted_b64,
                key_path=key_path,
                cert_path=cert_path,
                fault_stage=fault_stage,
            )
        ],
    )


def test_real_aws_lifecycle_success_preserves_unrelated_configuration(
    disposable_snapshot_bucket,
):
    client, bucket = disposable_snapshot_bucket

    assert snapshot_store.setup_lifecycle_rules() is True

    installed = _lifecycle(client, bucket)
    assert _UNRELATED_RULE in installed["Rules"]
    assert installed["TransitionDefaultMinimumObjectSize"] == _TRANSITION_DEFAULT
    assert len(installed["Rules"]) == 7


@pytest.mark.parametrize("failure", ["restart", "smoke"])
def test_real_aws_forced_failure_restores_exact_lifecycle(
    disposable_snapshot_bucket,
    failure,
):
    client, bucket = disposable_snapshot_bucket
    before = _lifecycle(client, bucket)

    try:
        assert snapshot_store.setup_lifecycle_rules() is True
        raise RuntimeError(f"forced {failure} failure")
    except RuntimeError:
        installer.restore_lifecycle_configuration(client, bucket, before)

    assert _lifecycle(client, bucket) == before


@pytest.mark.asyncio
async def test_real_aws_committed_upload_response_failure_leaves_no_version(
    disposable_snapshot_bucket,
):
    client, bucket = disposable_snapshot_bucket

    async def committed_upload(*_args, **kwargs):
        key = snapshot_store.build_s3_key(
            kwargs["university_id"],
            kwargs["scrape_job_id"],
            kwargs["url"],
            kwargs["snapshot_type"],
        )
        client.put_object(Bucket=bucket, Key=key, Body=b"committed-before-error")
        return None

    store = SimpleNamespace(
        is_enabled=lambda: True,
        build_s3_key=snapshot_store.build_s3_key,
        upload_snapshot=committed_upload,
        download_snapshot=snapshot_store.download_snapshot,
        _make_async_session=snapshot_store._make_async_session,
    )
    with pytest.raises(AssertionError):
        await installer.snapshot_round_trip(store)


@pytest.mark.asyncio
async def test_real_aws_transient_cleanup_failures_remove_exact_version(
    disposable_snapshot_bucket,
    monkeypatch,
):
    real_factory = snapshot_store._make_async_session
    failures_left = 2

    class _FaultClient:
        def __init__(self, client):
            self._client = client

        def __getattr__(self, name):
            return getattr(self._client, name)

        async def delete_objects(self, **kwargs):
            nonlocal failures_left
            if failures_left:
                failures_left -= 1
                raise RuntimeError("forced transient cleanup failure")
            return await self._client.delete_objects(**kwargs)

    class _FaultContext:
        def __init__(self, context):
            self._context = context

        async def __aenter__(self):
            return _FaultClient(await self._context.__aenter__())

        async def __aexit__(self, *args):
            return await self._context.__aexit__(*args)

    class _FaultSession:
        def client(self, *args, **kwargs):
            return _FaultContext(real_factory().client(*args, **kwargs))

    monkeypatch.setattr(snapshot_store, "_make_async_session", _FaultSession)

    with pytest.raises(RuntimeError, match="post-cleanup"):
        await installer.snapshot_round_trip(snapshot_store, fault_stage="cleanup")
    assert failures_left == 0


@pytest.mark.parametrize("fault_stage", ["restart", "upload-response", "cleanup"])
def test_disposable_ssm_host_forced_failure_restores_host_and_bucket_state(
    disposable_snapshot_bucket,
    fault_stage,
):
    instance_id = os.environ.get("SNAPSHOT_STORAGE_AWS_TEST_INSTANCE_ID")
    if not instance_id:
        pytest.skip("requires SNAPSHOT_STORAGE_AWS_TEST_INSTANCE_ID")
    client, bucket = disposable_snapshot_bucket
    session = _validated_ssm_fixture(instance_id)
    ssm = session.client(
        "ssm",
        region_name=os.environ.get("AWS_S3_REGION", "us-east-1"),
    )
    host_before = _remote_host_state(ssm, instance_id)
    lifecycle_before = _lifecycle(client, bucket)

    with pytest.raises(RuntimeError):
        _run_remote_transaction(instance_id, fault_stage, session)

    assert _remote_host_state(ssm, instance_id) == host_before
    assert _lifecycle(client, bucket) == lifecycle_before
