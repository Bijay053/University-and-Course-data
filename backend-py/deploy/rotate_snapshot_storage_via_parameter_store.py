#!/usr/bin/env python3
"""Rotate production snapshot storage through encrypted Parameter Store.

Secret values travel only in authenticated AWS API request bodies. They never
enter argv, SSM command parameters/history, logs, or program output.
"""
from __future__ import annotations

import argparse
import json
import os
import time

import boto3

from install_openai_fallback_via_ssm import _session

CONFIGURATION_PARAMETER = "/university-portal/snapshot-storage/configuration"
ROTATION_DOCUMENT = "university-portal-snapshot-storage-rotation"
REQUIRED_ENV = {
    "bucket_name": "AWS_S3_BUCKET_NAME",
    "region": "AWS_S3_REGION",
    "access_key_id": "AWS_ACCESS_KEY_ID",
    "secret_access_key": "AWS_SECRET_ACCESS_KEY",
}


def _configuration_from_environment() -> str:
    values = {
        name: os.environ.get(env_name)
        for name, env_name in REQUIRED_ENV.items()
    }
    missing = [REQUIRED_ENV[name] for name, value in values.items() if not value]
    if missing:
        raise SystemExit(
            "Missing required snapshot-storage secrets: " + ", ".join(missing)
        )
    values["endpoint_url"] = os.environ.get("AWS_S3_ENDPOINT_URL", "")
    for name, value in values.items():
        assert value is not None
        if "\n" in value or "\r" in value:
            raise SystemExit(f"Value for {name} must not contain a newline")
    return json.dumps(
        {"schema_version": 1, **values},
        separators=(",", ":"),
        sort_keys=True,
    )


def _run_rotation_document(ssm, instance_id: str, parameter_version: int) -> None:
    response = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName=ROTATION_DOCUMENT,
        Comment="Rotate university portal snapshot storage",
        Parameters={"ExpectedVersion": [str(parameter_version)]},
    )
    command_id = response["Command"]["CommandId"]
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            result = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
            )
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if result["Status"] in {"Pending", "InProgress", "Delayed"}:
            continue
        if result["Status"] != "Success":
            raise RuntimeError(
                f"Snapshot storage rotation failed with {result['Status']}"
            )
        if result.get("StandardOutputContent", "").strip() != (
            "rotated-and-roundtrip-verified"
        ):
            raise RuntimeError("Production rotation returned an unexpected response")
        return
    raise TimeoutError("Snapshot storage rotation did not finish within 180 seconds")


def rotate(instance_id: str, region: str, key_id: str) -> None:
    session: boto3.Session = _session()
    ssm = session.client("ssm", region_name=region)
    response = ssm.put_parameter(
        Name=CONFIGURATION_PARAMETER,
        Value=_configuration_from_environment(),
        Type="SecureString",
        KeyId=key_id,
        Overwrite=True,
        Tier="Standard",
    )
    _run_rotation_document(ssm, instance_id, int(response["Version"]))
    print("Snapshot storage rotated; both services and round-trip verified.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", default="ap-south-1")
    parser.add_argument(
        "--key-id",
        default="alias/university-portal-snapshot-storage",
        help="Customer-managed KMS key alias or ARN",
    )
    args = parser.parse_args()
    rotate(args.instance_id, args.region, args.key_id)


if __name__ == "__main__":
    main()