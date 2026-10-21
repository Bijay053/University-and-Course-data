"""Opt-in, billable STORAGE ONLY test. Never create a release smoke sample."""
import json
from contextlib import closing
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from botocore.config import Config

from deploy.archive_storage_proof import validate_proof
from deploy.rehearse_archive_storage import Resources, session

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DISPOSABLE_AWS_ARCHIVE_STORAGE") != "1",
    reason="requires separate approval and explicit disposable AWS archive opt-in",
)


@pytest.mark.parametrize("failure", [None, "after-stack-creation", "after-upload"])
def test_archive_storage_and_independent_cleanup(failure):
    required = (
        "DISPOSABLE_AWS_ACCESS_KEY_ID", "DISPOSABLE_AWS_SECRET_ACCESS_KEY",
        "DISPOSABLE_AWS_ACCOUNT_ID", "PRODUCTION_AWS_ACCOUNT_ID",
        "DISPOSABLE_AWS_REGION", "DISPOSABLE_AWS_PROOF_SIGNING_KEY_ID",
        "ARCHIVE_STORAGE_SIGNERS_PATH", "ARCHIVE_STORAGE_PROOF_DIRECTORY",
    )
    assert all(os.environ.get(key) for key in required), "explicit disposable settings required"
    account = os.environ["DISPOSABLE_AWS_ACCOUNT_ID"]
    production = os.environ["PRODUCTION_AWS_ACCOUNT_ID"]
    region = os.environ["DISPOSABLE_AWS_REGION"]
    signers = Path(os.environ["ARCHIVE_STORAGE_SIGNERS_PATH"]).resolve()
    # Operator-selected persistent CI artifact directory, NOT pytest tmp_path.
    output = Path(os.environ["ARCHIVE_STORAGE_PROOF_DIRECTORY"]).resolve()
    assert output.is_dir()
    run_id = uuid.uuid4().hex[:16]
    proof = output / f"archive-{run_id}-proof.json"
    state = output / f"archive-{run_id}-state.json"
    command = [
        sys.executable, "-m", "deploy.rehearse_archive_storage",
        "--expected-account-id", account, "--production-account-id", production,
        "--region", region, "--run-id", run_id,
        "--proof-signing-key-id", os.environ["DISPOSABLE_AWS_PROOF_SIGNING_KEY_ID"],
        "--signers", str(signers), "--proof-output", str(proof), "--state-output", str(state),
        "--i-understand-this-creates-disposable-aws-resources",
    ]
    if failure:
        command += ["--fail-at", failure]
    process_args = dict(cwd=Path(__file__).resolve().parents[1],
                        capture_output=True, text=True, check=False)
    timed_out = False
    try:
        result = subprocess.run(command, timeout=2400, **process_args)
    except subprocess.TimeoutExpired:
        # subprocess.run kills the child on timeout, bypassing its finally.
        # Recover using the saved identity, then independently check absence.
        timed_out = True
        assert state.is_file(), "timed out before provisioning state was written"
        result = subprocess.run(command + ["--cleanup-only"], timeout=900, **process_args)
    # A nonzero process status is not cleanup evidence.
    assert state.is_file(), (
        f"archive harness exited before writing resource state (exit={result.returncode}); "
        f"safe harness output: {result.stdout.strip()}"
    )
    saved = json.loads(state.read_text())
    assert saved["cleanup_verified"] is True
    sdk = session()
    config = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 2},
                    ignore_configured_endpoint_urls=True)
    with (closing(sdk.client("sts", region_name=region, config=config)) as sts,
          closing(sdk.client("s3", region_name=region, config=config)) as s3,
          closing(sdk.client("cloudformation", region_name=region, config=config)) as cf):
        Resources(sts=sts, s3=s3, cf=cf, account=account, production=production,
                  region=region, run_id=run_id).assert_absent()
    assert not timed_out, "archive rehearsal timed out; ownership-checked cleanup verified"
    if failure:
        assert result.returncode == 1
        assert saved.get("reached_checkpoint") == failure
        assert not proof.exists()
    else:
        assert result.returncode == 0
        validate_proof(proof, account=account, run_id=run_id, signers=signers)
        assert proof.stat().st_mode & 0o777 == 0o600
