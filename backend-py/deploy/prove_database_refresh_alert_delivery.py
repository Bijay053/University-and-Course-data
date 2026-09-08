#!/usr/bin/env python3
"""Prove the database-refresh alert-delivery alarm without touching its DLQ."""
from __future__ import annotations

import argparse
import os
import time

import boto3


ALARM_NAME = "university-portal-database-refresh-alert-delivery-failed"
TEST_REASON = "Disposable database refresh alert delivery proof"
RESTORE_REASON = "Restored after disposable database refresh alert delivery proof"


def _session():
    access_key = os.environ.get("AWS_SSM_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SSM_SECRET_ACCESS_KEY")
    if bool(access_key) != bool(secret_key):
        raise RuntimeError(
            "AWS_SSM_ACCESS_KEY_ID and AWS_SSM_SECRET_ACCESS_KEY must be set together"
        )
    kwargs = {}
    if access_key:
        kwargs.update(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
    return boto3.Session(**kwargs)


def _state(client) -> str:
    response = client.describe_alarms(AlarmNames=[ALARM_NAME])
    alarms = response.get("MetricAlarms", [])
    if len(alarms) != 1 or alarms[0].get("AlarmName") != ALARM_NAME:
        raise RuntimeError("fixed database refresh delivery alarm was not found uniquely")
    return alarms[0]["StateValue"]


def prove(region: str, timeout_seconds: int = 20) -> None:
    client = _session().client("cloudwatch", region_name=region)
    original = _state(client)
    if original == "ALARM":
        raise RuntimeError(
            "database refresh delivery alarm is active; investigate instead of testing"
        )
    try:
        client.set_alarm_state(
            AlarmName=ALARM_NAME,
            StateValue="ALARM",
            StateReason=TEST_REASON,
        )
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if _state(client) == "ALARM":
                print("database-refresh-alert-delivery-alarm-proved")
                return
            time.sleep(1)
        raise TimeoutError("database refresh delivery alarm did not enter ALARM")
    finally:
        client.set_alarm_state(
            AlarmName=ALARM_NAME,
            StateValue=original,
            StateReason=RESTORE_REASON,
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=20)
    args = parser.parse_args()
    prove(args.region, args.timeout_seconds)


if __name__ == "__main__":
    main()