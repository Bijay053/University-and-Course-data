#!/usr/bin/env python3
"""Prove the sanitized database refresh failure notification and deduplication path."""
from __future__ import annotations

import argparse
import json
import os
import time
import uuid

import boto3


DEFAULT_FUNCTION = "university-portal-database-refresh-failure-notifier"
DEFAULT_DOCUMENT = "university-portal-database-credential-refresh"
DEFAULT_TABLE = "university-portal-database-refresh-alert-deduplication"
TEST_SOURCE = "university-portal.database-refresh-alert-test"
TEST_DETAIL_TYPE = "Database Refresh Alert Test"


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


def prove(
    instance_id: str,
    region: str,
    table_name: str = DEFAULT_TABLE,
    timeout_seconds: int = 30,
) -> None:
    session = _session()
    events = session.client("events", region_name=region)
    ddb = session.client("dynamodb", region_name=region)
    nonce = uuid.uuid4().hex
    detail = {
        "instance-id": instance_id,
        "document-name": DEFAULT_DOCUMENT,
        "status": "Failed",
        "test-nonce": nonce,
    }
    entry = {
        "Source": TEST_SOURCE,
        "DetailType": TEST_DETAIL_TYPE,
        "Detail": json.dumps(detail),
    }
    response = events.put_events(Entries=[entry, entry])
    if response.get("FailedEntryCount") != 0:
        raise RuntimeError("database refresh alert test event was rejected")

    alert_key = "|".join((instance_id, DEFAULT_DOCUMENT, f"test:{nonce}"))
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        item = ddb.get_item(
            TableName=table_name,
            Key={"alert_key": {"S": alert_key}},
            ConsistentRead=True,
        ).get("Item", {})
        if (
            "published_at" in item
            and int(item.get("suppressed_count", {}).get("N", "0")) >= 1
        ):
            print("database-refresh-alert-published-and-repeat-suppressed")
            return
        time.sleep(1)
    raise TimeoutError(
        "database refresh alert proof did not observe publish and repeat suppression"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--table-name", default=DEFAULT_TABLE)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    args = parser.parse_args()
    prove(args.instance_id, args.region, args.table_name, args.timeout_seconds)


if __name__ == "__main__":
    main()