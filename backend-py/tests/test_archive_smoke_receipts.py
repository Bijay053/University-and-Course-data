"""Offline archive tests: no sample runner, real storage or credentials."""
import hashlib
import io
import json
import os

import pytest
from botocore.exceptions import ClientError

from deploy import archive_smoke_receipts as archive


def error(code):
    return ClientError({"Error": {"Code": code, "Message": "secret-provider-text"}}, "test")


class Storage:
    def __init__(self):
        self.objects = {}
        self.puts = 0
        self.calls = []
        self.fail_put = False
        self.corrupt = False
        self.expire = False
        self.versioning = "Enabled"
        self.public = False
        self.rules = []

    def _args(self, args):
        assert args["Bucket"] == "approved-bucket"
        assert args["ExpectedBucketOwner"] == "123456789012"
        self.calls.append(args)

    def get_public_access_block(self, **args):
        self._args(args)
        return {"PublicAccessBlockConfiguration": {
            name: not self.public for name in (
                "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets",
            )
        }}

    def get_bucket_versioning(self, **args):
        self._args(args)
        return {"Status": self.versioning}

    def get_bucket_lifecycle_configuration(self, **args):
        self._args(args)
        return {"Rules": self.rules}

    def put_object(self, **args):
        self._args(args)
        self.puts += 1
        assert args["IfNoneMatch"] == "*"
        assert args["ServerSideEncryption"] == "AES256"
        assert "ACL" not in args and "Tagging" not in args
        if self.fail_put:
            raise error("AccessDenied")
        if args["Key"] in self.objects:
            raise error("PreconditionFailed")
        self.objects[args["Key"]] = dict(args)
        return {"VersionId": "version-1"}

    def get_object(self, **args):
        self._args(args)
        if args["Key"] not in self.objects or (self.expire and "VersionId" in args):
            raise error("NoSuchKey")
        stored = self.objects[args["Key"]]
        return {"Body": io.BytesIO(b"corrupt" if self.corrupt else stored["Body"]),
                "Metadata": stored["Metadata"], "VersionId": "version-1"}


def receipt(directory, number):
    job = f"safe_restart_smoke_{number:032x}"
    path = directory / f"{job}.json"
    path.write_text(json.dumps({
        "kind": "ordinary_smoke_acceptance", "schema_version": 1,
        "database": {"runtime_job_id": job},
        "celery": {"job_id": job},
        "nested": {"token": "private-value", "url": "https://user:password@host/path?key=secret"},
    }))
    os.utime(path, (number, number))
    return path


def run(storage, directory, keep=1):
    return archive.archive_directory(storage, directory=directory,
                                     bucket="approved-bucket", owner="123456789012", keep=keep)


def test_failure_retry_and_idempotence_never_require_sample(tmp_path):
    storage = Storage()
    path = receipt(tmp_path, 1)
    storage.fail_put = True
    assert run(storage, tmp_path)["failed"] == 1
    assert path.exists()
    storage.fail_put = False
    assert run(storage, tmp_path)["archived"] == 1
    puts = storage.puts
    assert run(storage, tmp_path)["archived"] == 1
    assert storage.puts == puts
    uploaded = next(iter(storage.objects.values()))
    assert uploaded["Metadata"]["sha256"] == hashlib.sha256(uploaded["Body"]).hexdigest()
    assert b"private-value" not in uploaded["Body"]
    assert b"user:password" not in uploaded["Body"]
    assert b"key=secret" not in uploaded["Body"]


def test_retention_preserves_newest_and_verifies_old_versions(tmp_path):
    storage = Storage()
    paths = [receipt(tmp_path, number) for number in range(1, 5)]
    result = run(storage, tmp_path, keep=2)
    assert result == {"archived": 4, "failed": 0, "removed_local": 2, "retained_local": 2}
    assert [p.exists() for p in paths] == [False, False, True, True]
    assert len(storage.objects) == 4
    assert any(call.get("VersionId") == "version-1" for call in storage.calls)


@pytest.mark.parametrize("failure", ["upload", "corrupt", "vanished", "metadata"])
def test_no_prune_without_fresh_verified_copy(tmp_path, failure):
    storage = Storage()
    paths = [receipt(tmp_path, n) for n in (1, 2)]
    if failure == "upload":
        storage.fail_put = True
    elif failure == "corrupt":
        storage.corrupt = True
    elif failure == "vanished":
        storage.expire = True
    else:
        run(storage, tmp_path, keep=2)
        for obj in storage.objects.values():
            obj["Metadata"] = {"sha256": "wrong"}
    assert run(storage, tmp_path)["failed"] >= 1
    assert all(path.exists() for path in paths)


@pytest.mark.parametrize("bad", ["public", "unversioned", "expiration", "noncurrent", "prefix"])
def test_unsafe_storage_is_refused_before_upload(tmp_path, bad):
    storage = Storage()
    path = receipt(tmp_path, 1)
    if bad == "public":
        storage.public = True
    elif bad == "unversioned":
        storage.versioning = "Suspended"
    else:
        storage.rules = [{
            "Status": "Enabled",
            "NoncurrentVersionExpiration" if bad == "noncurrent" else "Expiration": {"Days": 1},
            "Filter": {"Prefix": archive.PREFIX if bad == "prefix" else ""},
        }]
    with pytest.raises(ValueError):
        run(storage, tmp_path)
    assert storage.puts == 0 and path.exists()


def test_snapshot_tag_lifecycle_does_not_apply_to_untagged_archive(tmp_path):
    storage = Storage()
    storage.rules = [{"Status": "Enabled", "Expiration": {"Days": 90},
                      "Filter": {"Tag": {"Key": "snapshot_type", "Value": "html"}}}]
    receipt(tmp_path, 1)
    assert run(storage, tmp_path)["archived"] == 1


def test_rejects_symlink_and_mismatched_identity(tmp_path):
    storage = Storage()
    first = receipt(tmp_path, 1)
    second = receipt(tmp_path, 2)
    second.write_text(first.read_text())
    third = tmp_path / f"safe_restart_smoke_{3:032x}.json"
    third.symlink_to(first)
    result = run(storage, tmp_path)
    assert result["failed"] == 2
    assert second.exists() and third.is_symlink()


def test_changed_local_file_is_never_removed(tmp_path, monkeypatch):
    storage = Storage()
    old = receipt(tmp_path, 1)
    receipt(tmp_path, 2)
    real = archive._verify
    def verify(client, args, data):
        value = real(client, args, data)
        if "VersionId" in args and old.stem in args["Key"]:
            old.write_text("changed")
        return value
    monkeypatch.setattr(archive, "_verify", verify)
    assert run(storage, tmp_path)["failed"] == 1
    assert old.read_text() == "changed"


def test_cli_redacts_provider_failures(monkeypatch, tmp_path, capsys):
    import boto3
    def fail(*args, **kwargs):
        raise RuntimeError("https://secret:credential@example.test")
    monkeypatch.setattr(boto3, "client", fail)
    assert archive.main(["--bucket", "approved-bucket", "--expected-owner", "123456789012",
                         "--region", "test-region", "--directory", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "secret" not in output and "credential" not in output
    assert "retry archival, not the sample" in output


def test_lost_upload_response_retry_reuses_remote_copy(tmp_path, monkeypatch):
    storage = Storage()
    path = receipt(tmp_path, 1)
    real = storage.put_object
    def lost_response(**args):
        real(**args)
        raise TimeoutError("response lost")
    monkeypatch.setattr(storage, "put_object", lost_response)
    assert run(storage, tmp_path)["failed"] == 1
    assert path.exists() and len(storage.objects) == 1
    assert run(storage, tmp_path)["failed"] == 0
    assert storage.puts == 1


def test_concurrent_conditional_write_reads_winning_copy(tmp_path, monkeypatch):
    storage = Storage()
    receipt(tmp_path, 1)
    real = storage.put_object
    def race(**args):
        real(**args)
        raise error("PreconditionFailed")
    monkeypatch.setattr(storage, "put_object", race)
    assert run(storage, tmp_path)["archived"] == 1
    assert len(storage.objects) == 1


def test_upload_request_matches_installed_sdk():
    import botocore.session
    shape = botocore.session.get_session().get_service_model("s3").operation_model("PutObject").input_shape
    assert {"IfNoneMatch", "ExpectedBucketOwner", "ServerSideEncryption", "Metadata"} <= set(shape.members)


@pytest.mark.parametrize("keep", [0, -1])
def test_invalid_retention_never_touches_storage(tmp_path, keep):
    storage = Storage()
    with pytest.raises(ValueError):
        run(storage, tmp_path, keep)
    assert storage.calls == []
