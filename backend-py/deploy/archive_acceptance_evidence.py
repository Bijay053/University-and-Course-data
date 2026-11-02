#!/usr/bin/env python3
"""Opt-in archive of reviewed fresh-course evidence, never operational logs."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

from botocore.exceptions import ClientError

PREFIX = "fresh-course-acceptance/v1/"
MAX_BYTES = 16 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|"
    r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]+|"
    r"X-Amz-(?:Credential|Signature|Security-Token)\s*[=:]|"
    r"(?:postgres(?:ql)?|redis)://[^\s\"<>]+:[^\s\"<>]+@|"
    r"\b(?:api[_-]?key|access[_-]?token|secret[_-]?key|password|"
    r"authorization|cookie)\s*[\"']?\s*[:=]\s*[\"']?[^\s\"'<>]{6,}",
    re.I,
)
FORBIDDEN_KEYS = re.compile(
    r"(?:env|environment|environment_variables|secrets?|credentials?|"
    r"logs?|stdout|stderr|password|api_key|access_token|authorization|cookie)", re.I
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def check_json(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if FORBIDDEN_KEYS.fullmatch(key):
                raise ValueError("forbidden receipt field")
            check_json(child)
    elif isinstance(value, list):
        for child in value:
            check_json(child)


def prepare(directory, inventory):
    """Only explicitly reviewed HTML and receipt JSON, with pinned original hashes."""
    if set(inventory) != {"schema_version", "acceptance_id", "files"}:
        raise ValueError("invalid inventory")
    if inventory["schema_version"] != 1 or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,95}", inventory["acceptance_id"]
    ):
        raise ValueError("invalid acceptance identity")
    if not isinstance(inventory["files"], list) or not 1 <= len(inventory["files"]) <= 100:
        raise ValueError("invalid file count")
    root = Path(directory)
    if root.is_symlink():
        raise ValueError("symlink directory")
    root = root.resolve(strict=True)
    prepared, seen, total = [], set(), 0
    for entry in inventory["files"]:
        if set(entry) != {"path", "sha256"}:
            raise ValueError("invalid file entry")
        relative = PurePosixPath(entry["path"])
        if (relative.is_absolute() or ".." in relative.parts
                or str(relative) != entry["path"] or not relative.parts
                or any(not re.fullmatch(r"[a-zA-Z0-9_-]+(?:\.html|\.json)?", p)
                       for p in relative.parts)
                or relative.suffix not in {".html", ".json"}
                or (relative.suffix == ".json" and relative.name != "receipt.json")
                or entry["path"] in seen
                or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])):
            raise ValueError("invalid evidence path or digest")
        seen.add(entry["path"])
        path = root
        for component in relative.parts:
            path = path / component
            if path.is_symlink():
                raise ValueError("symlink evidence")
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise ValueError("invalid evidence file")
        with path.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        total += len(data)
        if len(data) > MAX_BYTES or total > MAX_TOTAL or sha(data) != entry["sha256"]:
            raise ValueError("evidence size or hash mismatch")
        text = data.decode("utf-8")
        # Public form libraries map the field type "password" to itself.
        # This exact enum mapping is not a credential assignment.
        scan_text = re.sub(r'"password"\s*:\s*"password"', '"field_type":"field_type"', text)
        if SECRET.search(scan_text):
            raise ValueError("possible sensitive evidence; review locally")
        if relative.suffix == ".json":
            receipt = json.loads(text)
            if (not isinstance(receipt, dict)
                    or not set(receipt) <= {
                        "started_at", "completed_at", "isolation", "cases", "code_sha256"
                    }
                    or not {"started_at", "cases", "code_sha256"} <= set(receipt)
                    or not isinstance(receipt["cases"], list)):
                raise ValueError("not a fresh-course receipt")
            check_json(receipt)
        elif not re.search(r"<(?:html|!doctype html)\b", text, re.I):
            raise ValueError("not source HTML")
        prepared.append((entry, data))
    return prepared


def check_storage(client, bucket, owner):
    args = {"Bucket": bucket, "ExpectedBucketOwner": owner}
    block = client.get_public_access_block(**args)["PublicAccessBlockConfiguration"]
    if not all(block.get(k) is True for k in (
        "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"
    )):
        raise ValueError("private bucket required")
    if client.get_bucket_versioning(**args).get("Status") != "Enabled":
        raise ValueError("versioning required")
    try:
        rules = client.get_bucket_lifecycle_configuration(**args).get("Rules", [])
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchLifecycleConfiguration":
            raise
        rules = []
    for rule in rules:
        if rule.get("Status") != "Enabled" or not any(
            k in rule for k in ("Expiration", "NoncurrentVersionExpiration")
        ):
            continue
        selector = rule.get("Filter", {"Prefix": rule.get("Prefix", "")})
        prefix = selector.get("And", selector).get("Prefix", "")
        # Conservatively reject even tag/size-filtered expiration rules.
        if PREFIX.startswith(prefix) or prefix.startswith(PREFIX):
            raise ValueError("acceptance evidence subject to expiration")


def verify(client, args, data):
    response = client.get_object(**args)
    body = response["Body"]
    try:
        actual = body.read(len(data) + 1)
    finally:
        body.close()
    version = response.get("VersionId")
    if (not version or version == "null" or
            ("VersionId" in args and args["VersionId"] != version) or
            actual != data or response.get("Metadata", {}).get("sha256") != sha(data)):
        raise ValueError("remote integrity verification failed")
    return version


def store(client, args, data, content_type):
    try:
        response = client.put_object(
            **args, Body=data, ContentType=content_type,
            ServerSideEncryption="AES256", Metadata={"sha256": sha(data)},
            IfNoneMatch="*",
        )
        version = response.get("VersionId")
        if not version or version == "null":
            raise ValueError("upload missing immutable version identity")
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in {"PreconditionFailed", "412"}:
            raise
        version = verify(client, args, data)
    verify(client, {**args, "VersionId": version}, data)
    return {"key": args["Key"], "version_id": version, "sha256": sha(data),
            "size_bytes": len(data)}


def archive(client, *, directory, inventory, bucket, owner):
    if not bucket or not re.fullmatch(r"\d{12}", owner):
        raise ValueError("explicit bucket and owner required")
    prepared = prepare(directory, inventory)  # All checks before any remote writes.
    check_storage(client, bucket, owner)
    base = f"{PREFIX}{inventory['acceptance_id']}/{sha(encode(inventory))}/"
    args = {"Bucket": bucket, "ExpectedBucketOwner": owner}
    files = []
    for entry, data in prepared:
        proof = store(client, {**args, "Key": base + "files/" + entry["path"]}, data,
                      "text/html" if entry["path"].endswith(".html") else "application/json")
        files.append({"path": entry["path"], **proof})
    manifest = {"schema_version": 1, "kind": "fresh_course_acceptance",
                "acceptance_id": inventory["acceptance_id"], "bucket": bucket,
                "expected_owner": owner, "inventory_sha256": sha(encode(inventory)),
                "files": files}
    proof = store(client, {**args, "Key": base + "manifest.json"}, encode(manifest),
                  "application/json")
    return {"status": "retained", "bucket": bucket, "expected_owner": owner,
            "manifest": proof, "file_count": len(files)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upload", action="store_true", help="Explicit upload opt-in")
    parser.add_argument("--reviewed-safe", action="store_true",
                        help="Confirm listed files contain only public sources/test evidence")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--expected-owner", required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args(argv)
    try:
        if not args.upload or not args.reviewed_safe:
            raise ValueError("upload and review opt-in required")
        inventory = json.loads(args.inventory.read_text())
        prepare(args.directory, inventory)
        import boto3
        from botocore.config import Config
        client = boto3.client("s3", region_name=args.region, config=Config(
            connect_timeout=5, read_timeout=20, retries={"max_attempts": 2}))
        try:
            result = archive(client, directory=args.directory, inventory=inventory,
                             bucket=args.bucket, owner=args.expected_owner)
        finally:
            client.close()
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        # SDK errors and input contents must never enter operational output.
        print('{"status":"not_retained","error":"archive failed; local evidence unchanged"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
