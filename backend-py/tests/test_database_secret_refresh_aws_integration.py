"""Opt-in real-AWS coverage for the disposable database refresh rehearsal.

Never enable this in a production account.  CI must set both variables
deliberately; ordinary unit-test runs do not import credentials or mutate AWS.
"""
from __future__ import annotations

import os
import json
import subprocess
import sys
import uuid
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
import pytest

FAILURE_CHECKPOINTS = (
    "after-stack-creation",
    "after-probe-queueing",
    "after-password-rotation",
)
REGION = os.environ.get("DISPOSABLE_AWS_REGION", "ap-south-1")

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DISPOSABLE_AWS_DATABASE_REFRESH_REHEARSAL") != "1",
    reason="requires explicit disposable AWS opt-in",
)


def _command(tmp_path: Path, run_id: str, *extra: str) -> list[str]:
    required = (
        "DISPOSABLE_AWS_ACCESS_KEY_ID", "DISPOSABLE_AWS_SECRET_ACCESS_KEY",
        "DISPOSABLE_AWS_ACCOUNT_ID", "PRODUCTION_AWS_ACCOUNT_ID", "TEST_VPC_ID",
        "TEST_PRIVATE_SUBNET_A", "TEST_PRIVATE_SUBNET_B",
        "DISPOSABLE_AWS_PROOF_SIGNING_KEY_ID",
    )
    missing = [key for key in required if not os.environ.get(key)]
    assert not missing, f"missing explicit disposable integration settings: {', '.join(missing)}"
    deploy = Path(__file__).resolve().parents[1] / "deploy" / "rehearse_database_secret_refresh.py"
    return [
        sys.executable, str(deploy),
        "--expected-account-id", os.environ["DISPOSABLE_AWS_ACCOUNT_ID"],
        "--production-account-id", os.environ["PRODUCTION_AWS_ACCOUNT_ID"],
        "--vpc-id", os.environ["TEST_VPC_ID"],
        "--private-subnet-id", os.environ["TEST_PRIVATE_SUBNET_A"],
        "--second-private-subnet-id", os.environ["TEST_PRIVATE_SUBNET_B"],
        "--region", REGION,
        "--proof-output", str(tmp_path / "database-refresh-proof.json"),
        "--proof-signing-key-id", os.environ["DISPOSABLE_AWS_PROOF_SIGNING_KEY_ID"],
        "--run-id", run_id,
        "--state-output", str(tmp_path / "database-refresh-state.json"),
        "--i-understand-this-creates-disposable-aws-resources",
        *extra,
    ]


def _session() -> boto3.Session:
    return boto3.Session(
        aws_access_key_id=os.environ["DISPOSABLE_AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["DISPOSABLE_AWS_SECRET_ACCESS_KEY"],
        aws_session_token=os.environ.get("DISPOSABLE_AWS_SESSION_TOKEN") or None,
    )


def _assert_absent(tmp_path: Path, run_id: str) -> None:
    state = json.loads(
        (tmp_path / "database-refresh-state.json").read_text(encoding="utf-8")
    )
    assert state["run_id"] == run_id
    session = _session()

    with pytest.raises(ClientError) as stack_error:
        session.client("cloudformation", region_name=REGION).describe_stacks(
            StackName=state["stack_name"]
        )
    assert stack_error.value.response["Error"]["Code"] == "ValidationError"

    with pytest.raises(ClientError) as schedule_error:
        session.client("scheduler", region_name=REGION).get_schedule(
            Name=state["schedule_name"],
            GroupName=state["schedule_group"],
        )
    assert schedule_error.value.response["Error"]["Code"] == "ResourceNotFoundException"

    tagging = session.client("resourcegroupstaggingapi", region_name=REGION)
    residues = []
    token = ""
    while True:
        request = {
            "TagFilters": [
                {
                    "Key": "university-portal:disposable-rehearsal",
                    "Values": [run_id],
                }
            ]
        }
        if token:
            request["PaginationToken"] = token
        page = tagging.get_resources(**request)
        residues.extend(page["ResourceTagMappingList"])
        token = page.get("PaginationToken", "")
        if not token:
            break
    assert residues == []

    with pytest.raises(ClientError) as secret_error:
        session.client("secretsmanager", region_name=REGION).describe_secret(
            SecretId=state["secret_arn"]
        )
    assert (
        secret_error.value.response["Error"]["Code"] == "ResourceNotFoundException"
    )


def test_disposable_database_secret_refresh_rehearsal(tmp_path: Path) -> None:
    run_id = uuid.uuid4().hex[:16]
    subprocess.run(
        _command(tmp_path, run_id),
        check=True,
        timeout=2700,
    )
    assert (tmp_path / "database-refresh-proof.json").is_file()


@pytest.mark.parametrize("checkpoint", FAILURE_CHECKPOINTS)
def test_interrupted_rehearsal_leaves_no_billable_resources(
    tmp_path: Path, checkpoint: str
) -> None:
    run_id = uuid.uuid4().hex[:16]
    result = subprocess.run(
        _command(tmp_path, run_id, "--fail-at", checkpoint),
        check=False,
        capture_output=True,
        text=True,
        timeout=2700,
    )
    assert result.returncode != 0
    assert f"injected rehearsal failure at {checkpoint}" in result.stderr
    assert "rehearsal cleanup failed" not in result.stderr
    assert not (tmp_path / "database-refresh-proof.json").exists()
    _assert_absent(tmp_path, run_id)
