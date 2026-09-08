"""Opt-in real-AWS coverage for the disposable database refresh rehearsal.

Never enable this in a production account.  CI must set both variables
deliberately; ordinary unit-test runs do not import credentials or mutate AWS.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DISPOSABLE_AWS_DATABASE_REFRESH_REHEARSAL") != "1",
    reason="requires explicit disposable AWS opt-in",
)


def test_disposable_database_secret_refresh_rehearsal(tmp_path: Path) -> None:
    required = (
        "DISPOSABLE_AWS_ACCOUNT_ID", "PRODUCTION_AWS_ACCOUNT_ID", "TEST_VPC_ID",
        "TEST_PRIVATE_SUBNET_A", "TEST_PRIVATE_SUBNET_B",
        "DISPOSABLE_AWS_PROOF_SIGNING_KEY_ID",
    )
    missing = [key for key in required if not os.environ.get(key)]
    assert not missing, f"missing explicit disposable integration settings: {', '.join(missing)}"
    deploy = Path(__file__).resolve().parents[1] / "deploy" / "rehearse_database_secret_refresh.py"
    subprocess.run(
        [sys.executable, str(deploy),
         "--expected-account-id", os.environ["DISPOSABLE_AWS_ACCOUNT_ID"],
         "--production-account-id", os.environ["PRODUCTION_AWS_ACCOUNT_ID"],
         "--vpc-id", os.environ["TEST_VPC_ID"],
         "--private-subnet-id", os.environ["TEST_PRIVATE_SUBNET_A"],
         "--second-private-subnet-id", os.environ["TEST_PRIVATE_SUBNET_B"],
          "--proof-output", str(tmp_path / "database-refresh-proof.json"),
          "--proof-signing-key-id", os.environ["DISPOSABLE_AWS_PROOF_SIGNING_KEY_ID"],
         "--i-understand-this-creates-disposable-aws-resources"],
        check=True,
        timeout=2700,
    )
    assert (tmp_path / "database-refresh-proof.json").is_file()