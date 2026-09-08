#!/usr/bin/env python3
"""Request the fixed host-side refresh of the RDS-managed database secret.

This client deliberately has no database-secret argument or AWS secret read.
The EC2 instance role reads the stack-fixed Secrets Manager ARN directly and
only writes the resulting credential to its root-only environment file.
"""
from __future__ import annotations

import argparse
import time

from install_openai_fallback_via_ssm import _session


ROTATION_DOCUMENT = "university-portal-database-credential-refresh"
SUCCESS_OUTPUT = "database-credentials-refreshed-and-db-verified"


def refresh(instance_id: str, region: str) -> None:
    """Invoke only the fixed-purpose document and expose no command output."""
    ssm = _session().client("ssm", region_name=region)
    document = ssm.get_document(
        Name=ROTATION_DOCUMENT,
        DocumentVersion="$DEFAULT",
    )
    document_version = document["DocumentVersion"]
    document_content = document["Content"]
    response = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName=ROTATION_DOCUMENT,
        DocumentVersion=document_version,
        Comment="Refresh university portal RDS database credentials",
    )
    command_id = response["Command"]["CommandId"]
    deadline = time.monotonic() + 210
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            result = ssm.get_command_invocation(
                CommandId=command_id, InstanceId=instance_id
            )
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if result["Status"] in {"Pending", "InProgress", "Delayed"}:
            continue
        if result["Status"] != "Success":
            raise RuntimeError(
                f"Database credential refresh failed with {result['Status']}; "
                "the host attempted rollback. Inspect the fixed SSM command."
            )
        if result.get("StandardOutputContent", "").strip() != SUCCESS_OUTPUT:
            raise RuntimeError("Database credential refresh returned an unexpected response")
        verified = ssm.get_document(
            Name=ROTATION_DOCUMENT,
            DocumentVersion=document_version,
        )
        if (
            verified.get("DocumentVersion") != document_version
            or verified.get("Content") != document_content
        ):
            raise RuntimeError("Executed SSM document version failed content verification")
        print("Database credentials refreshed; API, Celery, and SELECT 1 verified.")
        return
    raise TimeoutError("Database credential refresh did not finish within 210 seconds")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", default="ap-south-1")
    args = parser.parse_args()
    refresh(args.instance_id, args.region)


if __name__ == "__main__":
    main()