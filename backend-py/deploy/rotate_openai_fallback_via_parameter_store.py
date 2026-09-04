#!/usr/bin/env python3
"""Rotate the production OpenAI fallback through encrypted Parameter Store.

Secret values are read from this process's environment and sent only in
authenticated AWS API request bodies. They are never command-line arguments,
SSM Run Command input/output, or program output.
"""
from __future__ import annotations

import argparse
import json
import os
import time

import boto3

from install_openai_fallback_via_ssm import _session


PARAMETER_PREFIX = "/university-portal/openai"
CONFIGURATION_PARAMETER = f"{PARAMETER_PREFIX}/configuration"
ROTATION_DOCUMENT = "university-portal-openai-rotation"


def _configuration_from_environment() -> str:
    direct_api_key = os.environ.get("OPENAI_API_KEY")
    if direct_api_key:
        api_key = direct_api_key
        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    else:
        api_key = os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY")
        base_url = os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL")

    values = {"api_key": api_key, "base_url": base_url}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise SystemExit(
            "Set OPENAI_API_KEY for external production, or provide both "
            "AI_INTEGRATIONS_OPENAI_API_KEY and AI_INTEGRATIONS_OPENAI_BASE_URL"
        )
    for name, value in values.items():
        assert value is not None
        if "\n" in value or "\r" in value:
            raise SystemExit(f"Value for {name} must not contain a newline")
    return json.dumps(values, separators=(",", ":"))


def _run_rotation_document(ssm, instance_id: str) -> None:
    response = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName=ROTATION_DOCUMENT,
        Comment="Rotate university portal OpenAI fallback",
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
            stderr = result.get("StandardErrorContent", "").strip()
            raise RuntimeError(
                f"OpenAI rotation failed with {result['Status']}: {stderr}"
            )
        if result.get("StandardOutputContent", "").strip() != "rotated":
            raise RuntimeError("Production rotation returned an unexpected response")
        return
    raise TimeoutError("OpenAI rotation did not finish within 180 seconds")


def rotate(instance_id: str, region: str, key_id: str) -> None:
    session: boto3.Session = _session()
    ssm = session.client("ssm", region_name=region)
    configuration = _configuration_from_environment()
    put_options = {
        "Value": configuration,
        "Type": "SecureString",
        "KeyId": key_id,
        "Overwrite": True,
        "Tier": "Standard",
    }

    ssm.put_parameter(Name=CONFIGURATION_PARAMETER, **put_options)
    _run_rotation_document(ssm, instance_id)
    print("OpenAI fallback rotated; both services are active.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", default="ap-south-1")
    parser.add_argument(
        "--key-id",
        default="alias/university-portal-openai",
        help="Customer-managed KMS key alias or ARN",
    )
    args = parser.parse_args()
    rotate(args.instance_id, args.region, args.key_id)


if __name__ == "__main__":
    main()