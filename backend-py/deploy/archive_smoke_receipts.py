#!/usr/bin/env python3
"""Archive existing smoke receipts only. Never import or run the sample runner."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path

from botocore.exceptions import ClientError

try:
    from .smoke_receipt import sanitize
except ImportError:
    from smoke_receipt import sanitize

PREFIX = "release-smoke-receipts/v1/"
MAX_BYTES = 8 * 1024 * 1024
NAME = re.compile(r"safe_restart_smoke_[a-f0-9]{32}\.json")


def _code(exc):
    return exc.response.get("Error", {}).get("Code")


def check_storage(client, bucket, owner):
    """Read-only preflight: never alter bucket permissions or lifecycle policies."""
    args = {"Bucket": bucket, "ExpectedBucketOwner": owner}
    block = client.get_public_access_block(**args)["PublicAccessBlockConfiguration"]
    if not all(block.get(key) is True for key in (
        "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets",
    )):
        raise ValueError("archive requires all bucket public-access blocks")
    if client.get_bucket_versioning(**args).get("Status") != "Enabled":
        raise ValueError("archive requires bucket versioning")
    try:
        rules = client.get_bucket_lifecycle_configuration(**args).get("Rules", [])
    except ClientError as exc:
        if _code(exc) != "NoSuchLifecycleConfiguration":
            raise
        rules = []
    # Conservative: tagged lifecycle rules do not match our untagged uploads.
    # Refuse ambiguous filters rather than risk removing the only durable copy.
    for rule in rules:
        if rule.get("Status") != "Enabled" or not any(
            field in rule for field in ("Expiration", "NoncurrentVersionExpiration")
        ):
            continue
        selector = rule.get("Filter", {"Prefix": rule.get("Prefix", "")})
        if "Tag" in selector or selector.get("And", {}).get("Tags"):
            continue
        prefix = selector.get("And", selector).get("Prefix", "")
        if PREFIX.startswith(prefix) or prefix.startswith(PREFIX):
            raise ValueError("archive prefix is subject to lifecycle expiration")


def _verify(client, args, expected):
    response = client.get_object(**args)
    body = response["Body"]
    try:
        actual = body.read(MAX_BYTES + 1)
    finally:
        body.close()
    digest = hashlib.sha256(expected).hexdigest()
    if actual != expected or response.get("Metadata", {}).get("sha256") != digest:
        raise ValueError("archive integrity verification failed")
    version = response.get("VersionId")
    if not version or version == "null":
        raise ValueError("archive has no durable version identity")
    return version


def archive_one(client, bucket, owner, path):
    if path.is_symlink() or not NAME.fullmatch(path.name):
        raise ValueError("invalid receipt path")
    with path.open("rb") as stream:
        original = stream.read(MAX_BYTES + 1)
    if len(original) > MAX_BYTES:
        raise ValueError("receipt too large")
    record = json.loads(original)
    if (
        record.get("kind") != "ordinary_smoke_acceptance"
        or record.get("schema_version") != 1
        or record.get("database", {}).get("runtime_job_id") != path.stem
        or record.get("celery", {}).get("job_id") != path.stem
    ):
        raise ValueError("receipt identity mismatch")
    # Redact again for receipts written by older helper versions.
    data = (json.dumps(sanitize(record), sort_keys=True, separators=(",", ":")) + "\n").encode()
    digest = hashlib.sha256(data).hexdigest()
    args = {"Bucket": bucket, "ExpectedBucketOwner": owner,
            "Key": f"{PREFIX}{path.stem}/{digest}.json"}
    try:
        version = _verify(client, args, data)
    except ClientError as exc:
        if _code(exc) not in ("NoSuchKey", "404"):
            raise
        try:
            response = client.put_object(
                **args, Body=data, ContentType="application/json",
                ServerSideEncryption="AES256", Metadata={"sha256": digest},
                IfNoneMatch="*",
            )
        except ClientError as upload_error:
            if _code(upload_error) not in ("PreconditionFailed", "412"):
                raise
            response = {}
        version_args = dict(args)
        if response.get("VersionId"):
            version_args["VersionId"] = response["VersionId"]
        version = _verify(client, version_args, data)
    return {"path": path, "original": original, "args": args,
            "version": version, "data": data, "sha256": digest}


def archive_directory(client, *, directory, bucket, owner, keep=50):
    """Failures retain local files; only freshly verified archived copies can age out."""
    if keep < 1 or not bucket or not re.fullmatch(r"\d{12}", owner):
        raise ValueError("explicit bucket, owner account and positive retention required")
    directory = Path(directory)
    check_storage(client, bucket, owner)
    lock_fd = os.open(directory / ".archive.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        paths = sorted(
            (p for p in directory.iterdir() if NAME.fullmatch(p.name)),
            key=lambda p: (p.lstat().st_mtime_ns, p.name), reverse=True,
        )
        result = {"archived": 0, "failed": 0, "removed_local": 0, "retained_local": len(paths)}
        for index, path in enumerate(paths):
            try:
                proof = archive_one(client, bucket, owner, path)
                result["archived"] += 1
                if index >= keep:
                    # Never trust a previous success marker or cached remote metadata.
                    _verify(client, {**proof["args"], "VersionId": proof["version"]}, proof["data"])
                    if path.is_symlink() or path.read_bytes() != proof["original"]:
                        raise ValueError("local receipt changed during archival")
                    path.unlink()
                    result["removed_local"] += 1
                    result["retained_local"] -= 1
            except Exception:
                # Provider errors may include credentials/URLs. Emit counts only.
                result["failed"] += 1
        return result
    finally:
        os.close(lock_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("/var/lib/university-portal/smoke-receipts"))
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--expected-owner", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--keep-local", type=int, default=50)
    args = parser.parse_args(argv)
    try:
        import boto3
        from botocore.config import Config
        # Existing AWS credential chain; no secrets are read, copied or printed here.
        client = boto3.client("s3", region_name=args.region, config=Config(
            connect_timeout=5, read_timeout=20, retries={"max_attempts": 2},
        ))
        try:
            result = archive_directory(client, directory=args.directory, bucket=args.bucket,
                                       owner=args.expected_owner, keep=args.keep_local)
        finally:
            client.close()
        print(json.dumps(result, sort_keys=True))
        return 1 if result["failed"] else 0
    except Exception:
        print('{"error":"archive unavailable; local receipts retained; retry archival, not the sample"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
