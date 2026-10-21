"""Separately approved, billable role rehearsal; ordinary test runs skip it."""
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from botocore.config import Config

from deploy.archive_storage_proof import validate_proof
from deploy.rehearse_archive_permissions import PermissionResources
from deploy.rehearse_archive_storage import session

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DISPOSABLE_AWS_ARCHIVE_PERMISSIONS") != "1",
    reason="requires separate disposable role provisioning and execution approval",
)


def test_restricted_archive_and_independent_cleanup():
    required = ("DISPOSABLE_AWS_ACCOUNT_ID", "PRODUCTION_AWS_ACCOUNT_ID",
                "DISPOSABLE_AWS_REGION", "DISPOSABLE_AWS_PROOF_SIGNING_KEY_ID",
                "ARCHIVE_STORAGE_SIGNERS_PATH", "ARCHIVE_STORAGE_PROOF_DIRECTORY")
    assert all(os.environ.get(k) for k in required), "explicit disposable settings required"
    account, production, region, key, pins, output = (os.environ[k] for k in required)
    output = Path(output).resolve()
    assert output.is_dir()
    run_id = uuid.uuid4().hex[:16]
    proof, state = (output / f"permissions-{run_id}-{suffix}.json" for suffix in ("proof", "state"))
    command = [
        sys.executable, "-m", "deploy.rehearse_archive_permissions",
        "--expected-account-id", account, "--production-account-id", production,
        "--region", region, "--run-id", run_id, "--proof-output", str(proof),
        "--state-output", str(state), "--proof-signing-key-id", key,
        "--signers", str(Path(pins).resolve()),
        "--i-understand-this-creates-disposable-aws-resources",
    ]
    timed_out = False
    try:
        result = subprocess.run(command, timeout=2400, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        timed_out = True
        assert state.is_file(), "no saved provisioning identity"
        result = subprocess.run(command + ["--cleanup-only"], timeout=900,
                                capture_output=True, text=True)
    assert json.loads(state.read_text())["cleanup_verified"] is True
    sdk = session()
    config = Config(ignore_configured_endpoint_urls=True, connect_timeout=5, read_timeout=20)
    with (closing(sdk.client("sts", region_name=region, config=config)) as sts,
          closing(sdk.client("s3", region_name=region, config=config)) as s3,
          closing(sdk.client("iam", region_name=region, config=config)) as iam,
          closing(sdk.client("cloudformation", region_name=region, config=config)) as cf):
        PermissionResources(sts=sts, s3=s3, iam=iam, cf=cf, account=account,
                            production=production, region=region, run_id=run_id).assert_absent()
    assert not timed_out, "test timed out; independently verified cleanup"
    assert result.returncode == 0, "permission rehearsal failed"
    validate_proof(proof, account=account, run_id=run_id, signers=pins, permissions=True)
