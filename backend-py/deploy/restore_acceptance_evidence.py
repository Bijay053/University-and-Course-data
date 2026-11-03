#!/usr/bin/env python3
"""Recover pinned fresh-course evidence; never run acceptance or mutate S3."""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath

try:
    from .archive_acceptance_evidence import PREFIX, MAX_BYTES, MAX_TOTAL, encode, sha
except ImportError:
    from archive_acceptance_evidence import PREFIX, MAX_BYTES, MAX_TOTAL, encode, sha

MAX_MANIFEST = 1024 * 1024
DIGEST = re.compile(r"[a-f0-9]{64}")
MANIFEST_KEY = re.compile(
    re.escape(PREFIX) + r"([a-z0-9][a-z0-9-]{0,95})/([a-f0-9]{64})/manifest\.json"
)
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class RestoreError(ValueError):
    """Only fixed, credential-safe reasons may cross the command boundary."""


def _version(value):
    return (isinstance(value, str) and value != "null"
            and 1 <= len(value) <= 1024 and all(33 <= ord(c) <= 126 for c in value))


def _selection(bucket, owner, region, manifest_key, version_id, sha256):
    if (not isinstance(bucket, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
            or not isinstance(owner, str) or not re.fullmatch(r"[0-9]{12}", owner)
            or not isinstance(region, str)
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+-[0-9]+", region)
            or not isinstance(manifest_key, str) or not MANIFEST_KEY.fullmatch(manifest_key)
            or not _version(version_id)
            or not isinstance(sha256, str) or not DIGEST.fullmatch(sha256)):
        raise RestoreError("invalid_explicit_selection")


def _download(client, args, *, key, version, digest, limit, size=None):
    response = client.get_object(**args, Key=key, VersionId=version)
    body = response["Body"]
    try:
        chunks, count = [], 0
        while count <= limit:
            chunk = body.read(min(65536, limit + 1 - count))
            if not chunk:
                break
            chunks.append(chunk)
            count += len(chunk)
        data = b"".join(chunks)
    finally:
        body.close()
    if (len(data) > limit or (size is not None and len(data) != size)
            or type(response.get("ContentLength")) is not int
            or response["ContentLength"] != len(data)
            or response.get("DeleteMarker")
            or response.get("VersionId") != version
            or response.get("Metadata", {}).get("sha256") != digest
            or sha(data) != digest):
        raise RestoreError("archive_integrity_failed")
    return data


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RestoreError("duplicate_manifest_field")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise RestoreError("invalid_manifest_number")


def _manifest(data, key, bucket, owner):
    value = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    identity, inventory_hash = MANIFEST_KEY.fullmatch(key).groups()
    if (not isinstance(value, dict) or set(value) != {
            "schema_version", "kind", "acceptance_id", "bucket", "expected_owner",
            "inventory_sha256", "files"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["kind"] != "fresh_course_acceptance"
            or value["acceptance_id"] != identity or value["bucket"] != bucket
            or value["expected_owner"] != owner or value["inventory_sha256"] != inventory_hash
            or not isinstance(value["files"], list) or not 1 <= len(value["files"]) <= 100):
        raise RestoreError("manifest_identity_mismatch")
    base = key.removesuffix("manifest.json")
    paths, total = set(), 0
    for entry in value["files"]:
        if not isinstance(entry, dict) or set(entry) != {
                "path", "key", "version_id", "sha256", "size_bytes"}:
            raise RestoreError("invalid_manifest_file")
        name = entry["path"]
        if not isinstance(name, str):
            raise RestoreError("unsafe_evidence_path")
        path = PurePosixPath(name)
        if (not path.parts or path.is_absolute() or str(path) != name
                or any(not re.fullmatch(r"[a-zA-Z0-9_-]+(?:\.html|\.json)?", part)
                       for part in path.parts)
                or path.suffix not in {".html", ".json"}
                or (path.suffix == ".json" and path.name != "receipt.json")
                or name in paths):
            raise RestoreError("unsafe_evidence_path")
        paths.add(name)
        if (entry["key"] != base + "files/" + name
                or not _version(entry["version_id"])
                or not isinstance(entry["sha256"], str) or not DIGEST.fullmatch(entry["sha256"])
                or type(entry["size_bytes"]) is not int
                or not 0 <= entry["size_bytes"] <= MAX_BYTES):
            raise RestoreError("invalid_manifest_file")
        total += entry["size_bytes"]
    if total > MAX_TOTAL:
        raise RestoreError("inventory_size_limit")
    if any(str(parent) in paths for name in paths for parent in PurePosixPath(name).parents):
        raise RestoreError("conflicting_evidence_paths")
    inventory = {"schema_version": 1, "acceptance_id": identity,
                 "files": [{"path": e["path"], "sha256": e["sha256"]} for e in value["files"]]}
    if sha(encode(inventory)) != inventory_hash:
        raise RestoreError("inventory_digest_mismatch")
    return value


def _parent(directory):
    """Walk every existing ancestor with no-follow, retaining an anchored parent FD."""
    path = Path(directory)
    if ".." in path.parts:
        raise RestoreError("unsafe_destination")
    path = path.absolute()
    if not path.name:
        raise RestoreError("unsafe_destination")
    fd = os.open(path.anchor, DIR_FLAGS)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd, path.name
    except BaseException:
        os.close(fd)
        raise


def _require_absent(parent, name):
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise RestoreError("destination_already_exists")


def _write(root, name, data):
    current = os.dup(root)
    try:
        parts = PurePosixPath(name).parts
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=current)
            except FileExistsError:
                pass
            child = os.open(part, DIR_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        fd = os.open(parts[-1], os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=current)
        with os.fdopen(fd, "w+b") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            stream.seek(0)
            if stream.read(len(data) + 1) != data:
                raise RestoreError("local_integrity_failed")
        os.fsync(current)
    finally:
        os.close(current)


def _verify_local(root, name, data):
    current = os.dup(root)
    try:
        parts = PurePosixPath(name).parts
        for part in parts[:-1]:
            child = os.open(part, DIR_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_size != len(data) or stream.read(len(data) + 1) != data):
                raise RestoreError("local_integrity_failed")
    finally:
        os.close(current)


def restore_evidence(client, *, directory, bucket, owner, region,
                     manifest_key, version_id, sha256):
    """Exact GetObject versions only; all remote validation precedes local creation."""
    try:
        _selection(bucket, owner, region, manifest_key, version_id, sha256)
        if client.meta.region_name != region:
            raise RestoreError("client_region_mismatch")
        parent, name = _parent(directory)
        try:
            _require_absent(parent, name)
            args = {"Bucket": bucket, "ExpectedBucketOwner": owner}
            manifest_data = _download(client, args, key=manifest_key, version=version_id,
                                      digest=sha256, limit=MAX_MANIFEST)
            manifest = _manifest(manifest_data, manifest_key, bucket, owner)
            downloaded = [(entry["path"], _download(
                client, args, key=entry["key"], version=entry["version_id"],
                digest=entry["sha256"], limit=entry["size_bytes"], size=entry["size_bytes"],
            )) for entry in manifest["files"]]
            # mkdir is exclusive, including against destinations created during the download.
            os.mkdir(name, 0o700, dir_fd=parent)
            root = os.open(name, DIR_FLAGS, dir_fd=parent)
            try:
                for relative, data in downloaded:
                    _write(root, relative, data)
                # Preserve the original verified manifest, not a new acceptance receipt.
                _write(root, ".acceptance-manifest.json", manifest_data)
                for relative, data in downloaded + [(".acceptance-manifest.json", manifest_data)]:
                    _verify_local(root, relative, data)
                os.fsync(root)
                if os.stat(name, dir_fd=parent, follow_symlinks=False).st_ino != os.fstat(root).st_ino:
                    raise RestoreError("destination_changed")
                current_parent, _ = _parent(directory)
                try:
                    old, current = os.fstat(parent), os.fstat(current_parent)
                    if (old.st_dev, old.st_ino) != (current.st_dev, current.st_ino):
                        raise RestoreError("destination_changed")
                finally:
                    os.close(current_parent)
                os.fsync(parent)
            finally:
                os.close(root)
        finally:
            os.close(parent)
        return {"operation": "restore_acceptance_evidence", "status": "restored",
                "new_acceptance": False, "acceptance_id": manifest["acceptance_id"],
                "file_count": len(downloaded), "bucket": bucket, "expected_owner": owner,
                "manifest": {"key": manifest_key, "version_id": version_id, "sha256": sha256}}
    except RestoreError:
        raise
    except Exception:
        raise RestoreError("restore_unavailable") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True,
                        help="New destination; parent must already exist without symlinks")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--expected-owner", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--manifest-key", required=True)
    parser.add_argument("--manifest-version-id", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        _selection(args.bucket, args.expected_owner, args.region, args.manifest_key,
                   args.manifest_version_id, args.manifest_sha256)
        import boto3
        from botocore.config import Config
        client = boto3.client("s3", region_name=args.region, config=Config(
            connect_timeout=5, read_timeout=20, retries={"max_attempts": 2}))
        try:
            result = restore_evidence(
                client, directory=args.directory, bucket=args.bucket, owner=args.expected_owner,
                region=args.region, manifest_key=args.manifest_key,
                version_id=args.manifest_version_id, sha256=args.manifest_sha256)
        finally:
            client.close()
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        # Never print SDK exceptions, manifest contents, credentials or local paths.
        print('{"operation":"restore_acceptance_evidence","status":"not_restored",'
              '"new_acceptance":false,"error":"restore failed; inspect destination locally '
              'for partial files and retry into a new directory"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
