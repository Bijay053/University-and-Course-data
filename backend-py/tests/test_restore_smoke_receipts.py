"""Hermetic restore coverage: no AWS, database, worker or smoke samples."""
import builtins
import hashlib
import io
import json
import os
import stat
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from deploy import archive_smoke_receipts as archive
from deploy import restore_smoke_receipts as restore
from deploy.smoke_receipt import save_receipt

JOB = "safe_restart_smoke_" + "a" * 32
OTHER_JOB = "safe_restart_smoke_" + "b" * 32
OWNER = "123456789012"
REGION = "eu-west-2"
SECRET = "https://user:secret@example.test/?token=private"


def record():
    return {
        "kind": "ordinary_smoke_acceptance", "schema_version": 1,
        "revision": "original-release",
        "database": {"runtime_job_id": JOB},
        "celery": {"job_id": JOB, "task_id": "original-task", "result": {"id": JOB}},
        "preflight": {"started_at": "2026-10-01T00:00:00+00:00",
                      "finished_at": "2026-10-01T00:01:00+00:00"},
        "worker_observed_at": "2026-10-01T00:00:59+00:00",
    }


class Storage:
    """Deliberately exposes no storage mutation methods."""

    def __init__(self):
        self.meta = SimpleNamespace(region_name=REGION)
        self.pages = [{"Versions": []}]
        self.objects = {}
        self.calls = []
        self.bodies = []
        self.closed = False

    def add(self, value=None, version="v1", data=None):
        data = data if data is not None else (json.dumps(value or record()) + "\n").encode()
        digest = hashlib.sha256(data).hexdigest()
        key = f"{archive.PREFIX}{JOB}/{digest}.json"
        self.pages[-1]["Versions"].append({"Key": key, "VersionId": version})
        self.objects[key, version] = {
            "data": data, "Metadata": {"sha256": digest}, "VersionId": version,
        }
        return key, version, digest

    def _check(self, method, args):
        assert args["Bucket"] == "approved-bucket"
        assert args["ExpectedBucketOwner"] == OWNER
        self.calls.append((method, args))

    def list_object_versions(self, **args):
        self._check("list", args)
        assert args["Prefix"] == f"{archive.PREFIX}{JOB}/"
        page = sum(method == "list" for method, _ in self.calls) - 1
        return self.pages[page]

    def get_object(self, **args):
        self._check("get", args)
        item = self.objects[args["Key"], args["VersionId"]]
        body = io.BytesIO(item["data"])
        self.bodies.append(body)
        return {**item, "Body": body}

    def close(self):
        self.closed = True


def run(storage, directory, **kwargs):
    return restore.restore_receipt(
        storage, directory=directory, **{
            "bucket": "approved-bucket", "owner": OWNER, "region": REGION, "job_id": JOB,
            **kwargs,
        },
    )


def test_restore_is_private_exact_and_idempotent(tmp_path):
    storage = Storage()
    key, version, digest = storage.add()
    directory = tmp_path / "replacement-host" / "receipts"
    result = run(storage, directory)
    path = directory / f"{JOB}.json"
    assert result["operation"] == "restore_smoke_receipt"
    assert result["status"] == "restored"
    assert result["new_acceptance"] is False and result["sha256"] == digest
    assert path.read_bytes() == storage.objects[key, version]["data"]
    assert json.loads(path.read_bytes()) == record()  # original acceptance times unchanged
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    before = path.stat()
    storage.calls.clear()
    assert run(storage, directory)["status"] == "already_present"
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert [method for method, _ in storage.calls] == ["list", "get"]
    assert all(body.closed for body in storage.bodies)
    assert not list(directory.glob(".restore-*"))


def test_missing_receipt_does_not_create_local_files(tmp_path):
    with pytest.raises(restore.RestoreError, match="receipt_not_found"):
        run(Storage(), tmp_path / "absent")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("same_hash", [False, True])
def test_multiple_receipt_versions_require_explicit_selection(tmp_path, same_hash):
    storage = Storage()
    first = storage.add()
    second = storage.add(record() if same_hash else {**record(), "revision": "different"}, "v2")
    with pytest.raises(restore.RestoreError, match="ambiguous"):
        run(storage, tmp_path)
    assert not any(method == "get" for method, _ in storage.calls)
    storage.calls.clear()
    if same_hash:
        with pytest.raises(restore.RestoreError, match="ambiguous"):
            run(storage, tmp_path, sha256=first[2])
        storage.calls.clear()
    result = run(storage, tmp_path, sha256=second[2], version_id=second[1])
    assert result["sha256"] == second[2]
    assert (tmp_path / f"{JOB}.json").read_bytes() == storage.objects[second[:2]]["data"]


def test_pagination_and_noncurrent_version_behind_delete_marker(tmp_path):
    storage = Storage()
    first = storage.add()
    storage.pages[0].update(IsTruncated=True, NextKeyMarker=first[0], NextVersionIdMarker="v1")
    storage.pages.append({"Versions": [], "DeleteMarkers": [
        {"Key": first[0], "VersionId": "delete-marker", "IsLatest": True},
    ]})
    second = storage.add({**record(), "revision": "second"}, "v2")
    result = run(storage, tmp_path, sha256=first[2], version_id="v1")
    assert result["sha256"] == first[2]
    assert storage.calls[1][1]["KeyMarker"] == first[0]
    assert storage.calls[1][1]["VersionIdMarker"] == "v1"
    assert storage.calls[2][1]["VersionId"] == "v1"
    assert second[0] != first[0]


@pytest.mark.parametrize("selection", [{"sha256": "0" * 64}, {"version_id": "missing"}])
def test_absent_selection_never_falls_back(tmp_path, selection):
    storage = Storage()
    storage.add()
    with pytest.raises(restore.RestoreError, match="receipt_not_found"):
        run(storage, tmp_path, **selection)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("damage", [
    "content", "metadata", "wrong_version", "null_version", "key", "oversize",
    "json", "duplicate_identity", "database", "celery", "result", "kind", "schema",
])
def test_corrupt_or_mismatched_receipts_never_publish(tmp_path, damage):
    storage = Storage()
    value = record()
    if damage in ("database", "celery", "result", "kind", "schema"):
        if damage == "database":
            value["database"]["runtime_job_id"] = OTHER_JOB
        elif damage == "celery":
            value["celery"]["job_id"] = OTHER_JOB
        elif damage == "result":
            value["celery"]["result"]["id"] = OTHER_JOB
        elif damage == "kind":
            value["kind"] = "later_inspection"
        else:
            value["schema_version"] = True
    data = None
    if damage == "json":
        data = b"not JSON"
    elif damage == "duplicate_identity":
        data = json.dumps(value).replace('"schema_version": 1', '"schema_version": 2, "schema_version": 1').encode()
    elif damage == "oversize":
        data = b" " * (archive.MAX_BYTES + 1)
    key, version, _ = storage.add(value, data=data)
    item = storage.objects[key, version]
    if damage == "content":
        item["data"] += b"corrupt"
    elif damage == "metadata":
        item["Metadata"] = {}
    elif damage == "wrong_version":
        item["VersionId"] = "not-requested"
    elif damage == "null_version":
        storage.pages[0]["Versions"][0]["VersionId"] = "null"
    elif damage == "key":
        storage.pages[0]["Versions"][0]["Key"] = key.replace(JOB, OTHER_JOB)
    with pytest.raises(restore.RestoreError):
        run(storage, tmp_path)
    assert list(tmp_path.iterdir()) == []
    assert all(body.closed for body in storage.bodies)


@pytest.mark.parametrize("existing", ["different", "public", "symlink", "directory", "fifo"])
def test_local_conflicts_are_never_overwritten(tmp_path, existing):
    storage = Storage()
    key, version, _ = storage.add()
    path = tmp_path / f"{JOB}.json"
    target = tmp_path / "untouched"
    target.write_bytes(b"private original")
    if existing in ("different", "public"):
        path.write_bytes(b"conflicting evidence" if existing == "different" else storage.objects[key, version]["data"])
        path.chmod(0o600 if existing == "different" else 0o644)
    elif existing == "symlink":
        path.symlink_to(target)
    elif existing == "directory":
        path.mkdir()
    else:
        os.mkfifo(path, 0o600)
    before = path.lstat()
    with pytest.raises(restore.RestoreError):
        run(storage, tmp_path)
    assert path.lstat() == before
    assert target.read_bytes() == b"private original"
    assert not list(tmp_path.glob(".restore-*"))


@pytest.mark.parametrize("same", [False, True])
def test_atomic_publish_handles_concurrent_local_writer(tmp_path, monkeypatch, same):
    storage = Storage()
    key, version, _ = storage.add()
    data = storage.objects[key, version]["data"]
    real_link = os.link
    def race(source, destination, **kwargs):
        path = tmp_path / destination
        path.write_bytes(data if same else b"concurrent evidence")
        path.chmod(0o600)
        return real_link(source, destination, **kwargs)
    monkeypatch.setattr(os, "link", race)
    if same:
        assert run(storage, tmp_path)["status"] == "already_present"
    else:
        with pytest.raises(restore.RestoreError, match="local_evidence_conflict"):
            run(storage, tmp_path)
    assert (tmp_path / f"{JOB}.json").read_bytes() == (data if same else b"concurrent evidence")
    assert not list(tmp_path.glob(".restore-*"))


def test_failed_fsync_cleans_temporary_without_publishing(tmp_path, monkeypatch):
    storage = Storage()
    storage.add()
    def fail(fd):
        raise OSError(SECRET)
    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(restore.RestoreError, match="restore_unavailable"):
        run(storage, tmp_path)
    assert not (tmp_path / f"{JOB}.json").exists()
    assert not list(tmp_path.glob(".restore-*"))


@pytest.mark.parametrize("unsafe", ["public", "symlink"])
def test_unsafe_directory_is_refused(tmp_path, unsafe):
    storage = Storage()
    storage.add()
    directory = tmp_path / "receipts"
    if unsafe == "public":
        directory.mkdir(mode=0o755)
    else:
        directory.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(restore.RestoreError):
        run(storage, directory)
    assert not (directory / f"{JOB}.json").exists()


@pytest.mark.parametrize("kwargs", [
    {"bucket": ""}, {"bucket": SECRET}, {"owner": "wrong"}, {"region": ""},
    {"job_id": "../other"}, {"sha256": "bad"}, {"version_id": "null"},
    {"region": "us-east-1"},
])
def test_bad_explicit_scope_never_contacts_storage(tmp_path, kwargs):
    storage = Storage()
    with pytest.raises(restore.RestoreError):
        run(storage, tmp_path, **kwargs)
    assert storage.calls == [] and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("phase", ["client", "list", "get", "close"])
def test_cli_failures_are_credential_safe(tmp_path, monkeypatch, capsys, phase):
    import boto3
    storage = Storage()
    storage.add()
    def fail(*args, **kwargs):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": SECRET}}, "test")
    monkeypatch.setattr(boto3, "client", fail if phase == "client" else lambda *a, **kw: storage)
    if phase != "client":
        monkeypatch.setattr(storage, {"list": "list_object_versions", "get": "get_object",
                                    "close": "close"}[phase], fail)
    assert restore.main([
        "--bucket", "approved-bucket", "--expected-owner", OWNER, "--region", REGION,
        "--job-id", JOB, "--directory", str(tmp_path),
    ]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "secret" not in captured.out and "private" not in captured.out
    assert "retry restoration, not the sample" in captured.out
    assert '"new_acceptance": false' in captured.out
    if phase in ("list", "get"):
        assert storage.closed
        assert not (tmp_path / f"{JOB}.json").exists()


def test_archive_to_replacement_host_round_trip_without_runner(tmp_path, monkeypatch, capsys):
    import boto3
    # Keep this test independent of pytest's prepend/importlib import modes.
    class ArchiveStorage:
        stored = None

        def get_object(self, **args):
            if self.stored is None:
                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            assert args["Key"] == self.stored["Key"]
            return {"Body": io.BytesIO(self.stored["Body"]),
                    "Metadata": self.stored["Metadata"], "VersionId": "v1"}

        def put_object(self, **args):
            assert args["ExpectedBucketOwner"] == OWNER
            assert args["IfNoneMatch"] == "*"
            self.stored = args
            return {"VersionId": "v1"}

    original = tmp_path / "original"
    original_record = save_receipt(
        original, release="original-release", job_id=JOB, task_id="task",
        payload={"status": "completed"}, staged_rows=[], counts={"running": 0},
        started_at="2026-10-01T00:00:00+00:00",
        worker_evidence={
            "task_id": "task", "job_id": JOB, "observed_at": "2026-10-01T00:01:00+00:00",
            "result": {"state": "SUCCESS", "value": {"ok": True, "id": JOB}},
            "complete_worker_replies": {"ping": {"worker": {"ok": "pong"}},
                                       "active": {"worker": []}, "reserved": {"worker": []},
                                       "scheduled": {"worker": []}},
        },
    )
    archived = ArchiveStorage()
    proof = archive.archive_one(archived, "approved-bucket", OWNER, original / f"{JOB}.json")
    storage = Storage()
    storage.add(data=proof["data"], version=proof["version"])
    monkeypatch.setattr(boto3, "client", lambda *a, **kw: storage)
    real_import = builtins.__import__
    def guard(name, *args, **kwargs):
        if any(part in name.split(".") for part in ("safe_restart_smoke", "celery", "app")):
            raise AssertionError("restore must not import runner or dispatch dependencies")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guard)
    # Re-execute imports too, not only the restore function's cached module.
    import importlib
    importlib.reload(restore)
    destination = tmp_path / "replacement"
    assert restore.main([
        "--bucket", "approved-bucket", "--expected-owner", OWNER, "--region", REGION,
        "--job-id", JOB, "--directory", str(destination),
    ]) == 0
    assert storage.closed
    assert json.loads((destination / f"{JOB}.json").read_bytes()) == original_record
    assert json.loads(capsys.readouterr().out)["new_acceptance"] is False


def test_repeated_pagination_marker_fails_closed(tmp_path):
    storage = Storage()
    key, _, _ = storage.add()
    storage.pages[0].update(IsTruncated=True, NextKeyMarker=key, NextVersionIdMarker="v1")
    storage.pages.append(storage.pages[0])
    with pytest.raises(restore.RestoreError, match="incomplete_version_listing"):
        run(storage, tmp_path)
    assert not any(method == "get" for method, _ in storage.calls)


@pytest.mark.parametrize("code", ["NoSuchKey", "NoSuchVersion", "AccessDenied"])
def test_object_disappears_after_listing_without_local_write(tmp_path, monkeypatch, code):
    storage = Storage()
    storage.add()
    def fail(**kwargs):
        raise ClientError({"Error": {"Code": code, "Message": SECRET}}, "GetObject")
    monkeypatch.setattr(storage, "get_object", fail)
    with pytest.raises(restore.RestoreError, match="^restore_unavailable$"):
        run(storage, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_failed_stream_read_closes_body_and_hides_provider_text(tmp_path, monkeypatch):
    storage = Storage()
    key, version, digest = storage.add()
    class FailedBody(io.BytesIO):
        def read(self, *args):
            raise OSError(SECRET)
    body = FailedBody()
    monkeypatch.setattr(storage, "get_object", lambda **kwargs: {
        "Body": body, "Metadata": {"sha256": digest}, "VersionId": version,
    })
    with pytest.raises(restore.RestoreError, match="^restore_unavailable$"):
        run(storage, tmp_path)
    assert body.closed and list(tmp_path.iterdir()) == []


def test_requests_match_installed_aws_sdk_without_network(tmp_path):
    import boto3
    from botocore.stub import Stubber
    # Explicit dummy values prevent the SDK from consulting any real credential chain.
    client = boto3.client("s3", region_name=REGION,
                          aws_access_key_id="offline", aws_secret_access_key="offline")
    data = json.dumps(record()).encode()
    digest = hashlib.sha256(data).hexdigest()
    key = f"{archive.PREFIX}{JOB}/{digest}.json"
    base = {"Bucket": "approved-bucket", "ExpectedBucketOwner": OWNER}
    try:
        with Stubber(client) as stub:
            stub.add_response("list_object_versions", {
                "Versions": [{"Key": key, "VersionId": "original-version"}], "IsTruncated": False,
            }, {**base, "Prefix": f"{archive.PREFIX}{JOB}/", "MaxKeys": 1000})
            stub.add_response("get_object", {
                "Body": io.BytesIO(data), "Metadata": {"sha256": digest},
                "VersionId": "original-version",
            }, {**base, "Key": key, "VersionId": "original-version"})
            assert run(client, tmp_path)["status"] == "restored"
            stub.assert_no_pending_responses()
    finally:
        client.close()
