"""Real-storage scenarios. Fault adapters delegate to S3 before injecting failures."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import uuid

from botocore.exceptions import ClientError

from . import archive_smoke_receipts as archive
from .archive_storage_proof import CHECKS


def require(condition, message):
    # Do not use assert: the harness must remain safe under python -O.
    if not condition:
        raise RuntimeError(message)


def receipt(directory, order):
    job = f"safe_restart_smoke_{uuid.uuid4().hex}"
    path = directory / f"{job}.json"
    path.write_text(json.dumps({
        "kind": "ordinary_smoke_acceptance", "schema_version": 1,
        "database": {"runtime_job_id": job}, "celery": {"job_id": job},
        "synthetic_archive_fixture": True,
    }))
    os.utime(path, (order, order))
    return path


def versions(client, args):
    return [
        item for page in client.get_paginator("list_object_versions").paginate(**args)
        for item in (*page.get("Versions", []), *page.get("DeleteMarkers", []))
    ]


class FaultClient:
    """Only the named fault is simulated; data and conflicts come from real S3."""
    def __init__(self, client, mode, target=None):
        self.client, self.mode, self.target = client, mode, target
        self.puts = 0
        self.conflicts = 0
        self.injected = 0

    def __getattr__(self, name):
        return getattr(self.client, name)

    def put_object(self, **args):
        self.puts += 1
        result = self.client.put_object(**args)
        if self.mode == "lost":
            self.injected += 1
            raise TimeoutError("injected lost response after committed S3 write")
        if self.mode == "race":
            # First write won the race; the second is a real S3 conditional conflict.
            try:
                self.client.put_object(**args)
            except ClientError as exc:
                require(archive._code(exc) in {"PreconditionFailed", "412"},
                        "S3 did not return the expected conditional conflict")
                self.conflicts += 1
                raise
            raise RuntimeError("S3 accepted duplicate conditional write")
        return result

    def get_object(self, **args):
        if (self.mode == "vanished" and self.target and self.target.stem in args["Key"]
                and "VersionId" in args):
            self.client.delete_object(**args)
            self.injected += 1
        result = self.client.get_object(**args)
        if self.target and self.target.stem in args["Key"] and "VersionId" in args:
            if self.mode == "changed":
                self.target.write_text("changed during verification")
                self.injected += 1
            elif self.mode == "unreadable":
                result["Body"].close()
                self.injected += 1
                raise TimeoutError("injected exact-version read failure")
        return result


def exercise(client, *, bucket, owner, directory, owned, checkpoint):
    """Run only synthetic local receipts; never import application or sample code."""
    args = {"Bucket": bucket, "ExpectedBucketOwner": owner}

    def folder(name):
        path = Path(directory) / name
        path.mkdir()
        return path

    def run(path, storage=client, keep=1):
        owned()
        return archive.archive_directory(storage, directory=path, bucket=bucket,
                                         owner=owner, keep=keep)

    def same_local(path, before):
        require({p.name: p.read_bytes() for p in path.glob("*.json")} == before,
                "failed operation changed local receipts")

    path = folder("conditional")
    first = receipt(path, 1)
    fault = FaultClient(client, "race")
    require(run(path, fault)["archived"] == 1 and fault.conflicts == 1,
            "real conditional conflict was not recovered")
    proof = archive.archive_one(client, bucket, owner, first)
    remote = client.get_object(**proof["args"], VersionId=proof["version"])
    try:
        body = remote["Body"].read()
    finally:
        remote["Body"].close()
    require(body == proof["data"] and hashlib.sha256(body).hexdigest() == proof["sha256"]
            and remote["Metadata"]["sha256"] == proof["sha256"]
            and remote.get("ServerSideEncryption") == "AES256", "readback mismatch")
    require(len(versions(client, {**args, "Prefix": proof["args"]["Key"]})) == 1,
            "conditional write created extra versions")
    checkpoint("after-upload")

    path = folder("lost-response")
    old, new = receipt(path, 1), receipt(path, 2)
    before = {p.name: p.read_bytes() for p in (old, new)}
    fault = FaultClient(client, "lost")
    require(run(path, fault)["failed"] == 2 and fault.injected == 2,
            "lost responses did not fail safely")
    same_local(path, before)
    before_versions = versions(client, args)
    require(run(path, fault) == {"archived": 2, "failed": 0, "removed_local": 1,
                               "retained_local": 1}, "lost response retry failed")
    require(fault.puts == 2 and versions(client, args) == before_versions,
            "retry uploaded duplicate versions")
    require(not old.exists() and new.exists(), "newest receipt not retained")

    path = folder("unsafe-config")
    receipt(path, 1)
    receipt(path, 2)
    before = {p.name: p.read_bytes() for p in path.glob("*.json")}
    before_versions = versions(client, args)
    for bad in ("public", "expiration", "noncurrent"):
        owned()
        if bad == "public":
            # No ACL/policy is granted. BlockPublicPolicy stays true throughout.
            block = {name: True for name in (
                "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets",
            )}
            client.put_public_access_block(**args, PublicAccessBlockConfiguration={
                **block, "IgnorePublicAcls": False,
            })
        else:
            expiry = ({"Expiration": {"Days": 1}} if bad == "expiration" else
                      {"NoncurrentVersionExpiration": {"NoncurrentDays": 1}})
            client.put_bucket_lifecycle_configuration(**args, LifecycleConfiguration={
                "Rules": [{"ID": "archive-refusal-fixture", "Status": "Enabled",
                           "Filter": {"Prefix": archive.PREFIX}, **expiry}],
            })
        try:
            try:
                run(path)
            except ValueError as exc:
                require(("public-access" if bad == "public" else "lifecycle") in str(exc),
                        "unexpected preflight refusal")
            else:
                raise RuntimeError("unsafe bucket was accepted")
            same_local(path, before)
            require(versions(client, args) == before_versions, "preflight refusal wrote objects")
        finally:
            owned()
            if bad == "public":
                client.put_public_access_block(**args, PublicAccessBlockConfiguration=block)
            else:
                client.delete_bucket_lifecycle(**args)

    for failure in ("body", "metadata", "vanished", "unreadable", "changed"):
        path = folder(f"retention-{failure}")
        old, new = receipt(path, 1), receipt(path, 2)
        require(run(path, keep=2)["archived"] == 2, "retention setup failed")
        proof = archive.archive_one(client, bucket, owner, old)
        if failure in {"body", "metadata"}:
            owned()
            client.put_object(**proof["args"], Body=b"corrupt" if failure == "body" else proof["data"],
                              Metadata={"sha256": "wrong" if failure == "metadata" else proof["sha256"]},
                              ServerSideEncryption="AES256")
        adapter = FaultClient(client, failure, old)
        result = run(path, adapter)
        require(result["failed"] == 1 and result["removed_local"] == 0
                and old.exists() and new.exists(), "unverified receipt was pruned")
        if failure in {"vanished", "unreadable", "changed"}:
            require(adapter.injected == 1, "retention fault not exercised")
    return CHECKS
