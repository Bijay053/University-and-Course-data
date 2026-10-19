#!/usr/bin/env python3
"""Restore archived acceptance evidence, read-only in S3; never run a sample."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path

try:
    from .archive_smoke_receipts import MAX_BYTES, PREFIX
except ImportError:
    from archive_smoke_receipts import MAX_BYTES, PREFIX

JOB = re.compile(r"safe_restart_smoke_[a-f0-9]{32}")
SHA256 = re.compile(r"[a-f0-9]{64}")


class RestoreError(ValueError):
    """Fixed, credential-safe failure reason; never include provider text."""


def _validate_inputs(bucket, owner, region, job_id, sha256, version_id):
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
        or not re.fullmatch(r"\d{12}", owner)
        or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+-\d+", region)
        or not JOB.fullmatch(job_id)
        or (sha256 is not None and not SHA256.fullmatch(sha256))
        or (version_id is not None and (not version_id or version_id == "null"))
    ):
        raise RestoreError("invalid_explicit_selection")


def _find_version(client, args, job_id, sha256, version_id):
    prefix = f"{PREFIX}{job_id}/"
    markers = {}
    seen_markers = set()
    candidates = set()
    # Bound even a malformed or unexpectedly large version listing.
    for _ in range(100):
        page = client.list_object_versions(**args, Prefix=prefix, MaxKeys=1000, **markers)
        for item in page.get("Versions", []):
            key = item.get("Key", "")
            match = re.fullmatch(re.escape(prefix) + r"([a-f0-9]{64})\.json", key)
            if not match:
                raise RestoreError("invalid_archive_key")
            digest = match.group(1)
            version = item.get("VersionId")
            if not version or version == "null":
                raise RestoreError("missing_version_identity")
            if sha256 is not None and digest != sha256:
                continue
            if version_id is not None and version != version_id:
                continue
            candidates.add((key, version, digest))
        # Delete markers are not receipt content. Noncurrent content can be recovered.
        if not page.get("IsTruncated"):
            break
        marker = (page.get("NextKeyMarker"), page.get("NextVersionIdMarker"))
        if not marker[0] or marker in seen_markers:
            raise RestoreError("incomplete_version_listing")
        seen_markers.add(marker)
        markers = {"KeyMarker": marker[0]}
        if marker[1]:
            markers["VersionIdMarker"] = marker[1]
    else:
        raise RestoreError("version_listing_limit")
    if not candidates:
        raise RestoreError("receipt_not_found")
    if len(candidates) != 1:
        raise RestoreError("ambiguous_receipt_select_sha256_and_version")
    return candidates.pop()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RestoreError("duplicate_json_key")
        result[key] = value
    return result


def _read_verified(client, args, key, version, digest, job_id):
    response = client.get_object(**args, Key=key, VersionId=version)
    body = response["Body"]
    try:
        data = body.read(MAX_BYTES + 1)
    finally:
        body.close()
    if (
        len(data) > MAX_BYTES
        or hashlib.sha256(data).hexdigest() != digest
        or response.get("Metadata", {}).get("sha256") != digest
        or response.get("VersionId") != version
    ):
        raise RestoreError("archive_integrity_failed")
    record = json.loads(data, object_pairs_hook=_unique_object)
    if (
        not isinstance(record, dict)
        or record.get("kind") != "ordinary_smoke_acceptance"
        or type(record.get("schema_version")) is not int
        or record["schema_version"] != 1
        or not isinstance(record.get("database"), dict)
        or record["database"].get("runtime_job_id") != job_id
        or not isinstance(record.get("celery"), dict)
        or record["celery"].get("job_id") != job_id
    ):
        raise RestoreError("receipt_identity_mismatch")
    # Earlier archives may lack the result, but a present result must agree.
    result = record["celery"].get("result")
    if result is not None and (not isinstance(result, dict) or result.get("id") != job_id):
        raise RestoreError("receipt_identity_mismatch")
    return data


def _private_regular(fd):
    info = os.fstat(fd)
    return (
        stat.S_ISREG(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_uid == os.geteuid()
    )


def _matches_existing(directory_fd, name, data):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(fd, "rb") as stream:
        if not _private_regular(stream.fileno()) or stream.read(MAX_BYTES + 1) != data:
            raise RestoreError("local_evidence_conflict")


def _install(directory, job_id, data):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory_fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise RestoreError("private_directory_required")
        lock_fd = os.open(".archive.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                          0o600, dir_fd=directory_fd)
        try:
            if not _private_regular(lock_fd):
                raise RestoreError("unsafe_local_lock")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            name = f"{job_id}.json"
            try:
                _matches_existing(directory_fd, name, data)
            except FileNotFoundError:
                pass
            else:
                return "already_present"
            temporary = f".restore-{uuid.uuid4().hex}"
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
            try:
                with os.fdopen(fd, "wb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    # Atomic no-replace publication, even against a non-cooperating writer.
                    os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                            follow_symlinks=False)
                except FileExistsError:
                    _matches_existing(directory_fd, name, data)
                    return "already_present"
                os.fsync(directory_fd)
                return "restored"
            finally:
                os.unlink(temporary, dir_fd=directory_fd)
        finally:
            os.close(lock_fd)
    finally:
        os.close(directory_fd)


def restore_receipt(client, *, directory, bucket, owner, region, job_id,
                    sha256=None, version_id=None):
    """Only version-list/get operations; preserve archived bytes, not fresh acceptance."""
    try:
        _validate_inputs(bucket, owner, region, job_id, sha256, version_id)
        if client.meta.region_name != region:
            raise RestoreError("client_region_mismatch")
        args = {"Bucket": bucket, "ExpectedBucketOwner": owner}
        key, version, digest = _find_version(client, args, job_id, sha256, version_id)
        data = _read_verified(client, args, key, version, digest, job_id)
        status = _install(directory, job_id, data)
        return {
            "operation": "restore_smoke_receipt", "status": status,
            "new_acceptance": False, "job_id": job_id, "sha256": digest,
            "restoration_checked_at": datetime.now(UTC).isoformat(),
        }
    except RestoreError:
        raise
    except Exception:
        # Provider/JSON/filesystem errors may embed credentials or receipt contents.
        raise RestoreError("restore_unavailable") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("/var/lib/university-portal/smoke-receipts"))
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--expected-owner", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--sha256", help="Select a content hash when multiple receipts exist")
    parser.add_argument("--version-id", help="Select an exact S3 object version")
    args = parser.parse_args(argv)
    try:
        _validate_inputs(args.bucket, args.expected_owner, args.region, args.job_id,
                         args.sha256, args.version_id)
        import boto3
        from botocore.config import Config
        client = boto3.client("s3", region_name=args.region, config=Config(
            connect_timeout=5, read_timeout=20, retries={"max_attempts": 2},
        ))
        try:
            result = restore_receipt(
                client, directory=args.directory, bucket=args.bucket, owner=args.expected_owner,
                region=args.region, job_id=args.job_id, sha256=args.sha256, version_id=args.version_id,
            )
        finally:
            client.close()
    except Exception as exc:
        reason = str(exc) if isinstance(exc, RestoreError) else "restore_unavailable"
        print(json.dumps({"error": reason, "new_acceptance": False,
                          "action": "retry restoration, not the sample"}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
