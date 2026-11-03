"""Offline, exact-version recovery; no AWS credentials, database or running app."""
import io
import json
import sys
from types import SimpleNamespace

import pytest

from deploy import archive_acceptance_evidence as archive
from deploy import restore_acceptance_evidence as restore

OWNER, BUCKET, REGION = "123456789012", "evidence-bucket", "eu-west-2"


class Storage:
    """No list, put, delete or bucket-mutation methods on the recovery client."""
    def __init__(self):
        self.meta = SimpleNamespace(region_name=REGION)
        self.objects, self.calls, self.bodies = {}, [], []
        self.closed = False

    def add(self, key, version, data):
        self.objects[key, version] = {
            "data": data, "VersionId": version, "Metadata": {"sha256": archive.sha(data)},
            "ContentLength": len(data),
        }

    def get_object(self, **args):
        assert set(args) == {"Bucket", "ExpectedBucketOwner", "Key", "VersionId"}
        assert args["Bucket"] == BUCKET and args["ExpectedBucketOwner"] == OWNER
        self.calls.append(args)
        obj = self.objects[args["Key"], args["VersionId"]]
        body = io.BytesIO(obj["data"])
        self.bodies.append(body)
        return {**{k: v for k, v in obj.items() if k != "data"}, "Body": body}

    def close(self):
        self.closed = True


@pytest.fixture
def evidence(tmp_path):
    files = {
        "initial/receipt.json": archive.encode({
            "started_at": "2026-11-02", "cases": [{"status": "failed"}], "code_sha256": {},
        }),
        "source/course.html": b"<!doctype html><html>Published course fees</html>",
    }
    inventory = {"schema_version": 1, "acceptance_id": "recovery-test",
                 "files": [{"path": p, "sha256": archive.sha(d)} for p, d in files.items()]}
    digest = archive.sha(archive.encode(inventory))
    base = f"{archive.PREFIX}recovery-test/{digest}/"
    manifest = {"schema_version": 1, "kind": "fresh_course_acceptance",
                "acceptance_id": "recovery-test", "bucket": BUCKET, "expected_owner": OWNER,
                "inventory_sha256": digest, "files": []}
    client = Storage()
    for i, (path, data) in enumerate(files.items()):
        key, version = base + "files/" + path, f"file-{i}-v1"
        client.add(key, version, data)
        manifest["files"].append({"path": path, "key": key, "version_id": version,
                                  "sha256": archive.sha(data), "size_bytes": len(data)})
    kwargs = dict(directory=tmp_path / "recovered", bucket=BUCKET, owner=OWNER, region=REGION,
                  manifest_key=base + "manifest.json", version_id="manifest-v1")
    pack = SimpleNamespace(client=client, files=files, manifest=manifest, kwargs=kwargs)
    pin(pack)
    return pack


def pin(pack, data=None):
    data = archive.encode(pack.manifest) if data is None else data
    pack.client.add(pack.kwargs["manifest_key"], pack.kwargs["version_id"], data)
    pack.kwargs["sha256"] = archive.sha(data)
    return data


def run(pack):
    return restore.restore_evidence(pack.client, **pack.kwargs)


def test_exact_versions_preserve_original_bytes_and_ignore_corrupt_latest(evidence):
    p = evidence
    for entry in p.manifest["files"]:
        p.client.add(entry["key"], "newer-version", b"newer corrupt bytes")
    p.client.add(p.kwargs["manifest_key"], "newer-version", b"bad newer manifest")
    result = run(p)
    assert result["status"] == "restored" and result["new_acceptance"] is False
    assert result["file_count"] == len(p.files)
    for name, data in p.files.items():
        path = p.kwargs["directory"] / name
        assert path.read_bytes() == data
        assert path.stat().st_mode & 0o777 == 0o600
    assert p.kwargs["directory"].stat().st_mode & 0o777 == 0o700
    assert (p.kwargs["directory"] / ".acceptance-manifest.json").read_bytes() == archive.encode(p.manifest)
    assert [c["VersionId"] for c in p.client.calls] == ["manifest-v1", "file-0-v1", "file-1-v1"]
    assert all(b.closed for b in p.client.bodies)


@pytest.mark.parametrize("field,value", [
    ("version_id", ""), ("version_id", "null"), ("sha256", "bad"),
    ("owner", ""), ("owner", "１２３４５６７８９０１２"),
    ("manifest_key", "release-smoke-receipts/v1/receipt.json"),
])
def test_explicit_selection_required_before_any_read(evidence, field, value):
    evidence.kwargs[field] = value
    with pytest.raises(restore.RestoreError):
        run(evidence)
    assert not evidence.client.calls
    assert not evidence.kwargs["directory"].exists()


@pytest.mark.parametrize("payload", [b"not json", b"{}", b"[]", b'{"schema_version":1,"schema_version":1}', b'{"size":NaN}'])
def test_hash_pinned_but_invalid_manifests_never_download_files(evidence, payload):
    pin(evidence, payload)
    with pytest.raises(restore.RestoreError):
        run(evidence)
    assert len(evidence.client.calls) == 1
    assert not evidence.kwargs["directory"].exists()


def test_manifest_hash_checked_before_parsing_or_file_reads(evidence):
    p = evidence
    p.client.objects[p.kwargs["manifest_key"], p.kwargs["version_id"]]["data"] = b"corrupt"
    with pytest.raises(restore.RestoreError, match="integrity"):
        run(p)
    assert len(p.client.calls) == 1 and all(b.closed for b in p.client.bodies)


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("kind", "ordinary_smoke_acceptance"),
    ("bucket", "other-bucket"), ("expected_owner", "999999999999"),
    ("acceptance_id", "another-run"), ("inventory_sha256", "a" * 64),
])
def test_rejects_manifest_identity_changes(evidence, field, value):
    evidence.manifest[field] = value
    pin(evidence)
    with pytest.raises(restore.RestoreError):
        run(evidence)
    assert len(evidence.client.calls) == 1


@pytest.mark.parametrize("path", [
    "../escape.html", "/escape.html", "a/../../escape.html", "a//course.html",
    "./course.html", "a/./course.html", "a\\course.html", "C:/course.html",
    "%2e%2e/escape.html", "a/\x00.html", "secret.json",
])
def test_path_traversal_and_noncanonical_paths_rejected_before_download(evidence, path):
    evidence.manifest["files"][0]["path"] = path
    pin(evidence)
    with pytest.raises(restore.RestoreError):
        run(evidence)
    assert len(evidence.client.calls) == 1
    assert not evidence.kwargs["directory"].exists()


@pytest.mark.parametrize("field,value", [
    ("key", "other-prefix/course.html"), ("version_id", None), ("version_id", "null"),
    ("size_bytes", -1), ("size_bytes", True), ("size_bytes", archive.MAX_BYTES + 1),
    ("sha256", "bad"), ("sha256", "a" * 64),
])
def test_invalid_file_metadata_and_inventory_hash_rejected(evidence, field, value):
    evidence.manifest["files"][0][field] = value
    pin(evidence)
    with pytest.raises(restore.RestoreError):
        run(evidence)
    assert len(evidence.client.calls) == 1


def test_duplicate_paths_rejected(evidence):
    evidence.manifest["files"].append(evidence.manifest["files"][0].copy())
    pin(evidence)
    with pytest.raises(restore.RestoreError):
        run(evidence)
    assert len(evidence.client.calls) == 1


@pytest.mark.parametrize("target", ["manifest", "file"])
def test_missing_exact_version_never_falls_back_to_latest(evidence, target):
    p = evidence
    key, version = (p.kwargs["manifest_key"], p.kwargs["version_id"]) if target == "manifest" else (
        p.manifest["files"][0]["key"], p.manifest["files"][0]["version_id"])
    p.client.add(key, "latest", p.client.objects[key, version]["data"])
    del p.client.objects[key, version]
    with pytest.raises(restore.RestoreError):
        run(p)
    assert not p.kwargs["directory"].exists()
    assert not any(c["VersionId"] == "latest" for c in p.client.calls)


@pytest.mark.parametrize("change", ["bytes", "size", "version", "metadata"])
def test_file_integrity_failure_leaves_no_destination(evidence, change):
    p = evidence
    entry = p.manifest["files"][-1]
    obj = p.client.objects[entry["key"], entry["version_id"]]
    if change == "bytes":
        obj["data"] = b"x" * len(obj["data"])
    elif change == "size":
        obj["ContentLength"] += 1
    elif change == "version":
        obj["VersionId"] = "wrong-version"
    else:
        obj["Metadata"] = {}
    with pytest.raises(restore.RestoreError, match="integrity"):
        run(p)
    assert not p.kwargs["directory"].exists()
    assert all(b.closed for b in p.client.bodies)


@pytest.mark.parametrize("kind", ["empty", "populated", "file", "symlink"])
def test_existing_destination_never_overwritten(evidence, tmp_path, kind):
    dest = evidence.kwargs["directory"]
    if kind in {"empty", "populated"}:
        dest.mkdir()
        if kind == "populated":
            (dest / "keep").write_bytes(b"existing data")
    elif kind == "file":
        dest.write_bytes(b"existing data")
    else:
        dest.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    with pytest.raises(restore.RestoreError, match="already_exists"):
        run(evidence)
    assert not evidence.client.calls
    if kind == "populated":
        assert (dest / "keep").read_bytes() == b"existing data"


def test_symlink_parent_is_rejected(evidence, tmp_path):
    link = tmp_path / "linked"
    link.symlink_to(tmp_path, target_is_directory=True)
    evidence.kwargs["directory"] = link / "recovered"
    with pytest.raises(restore.RestoreError):
        run(evidence)
    assert not evidence.client.calls


def test_destination_created_during_download_is_not_overwritten(evidence):
    original = evidence.client.get_object
    def race(**args):
        if args["Key"].endswith("manifest.json"):
            evidence.kwargs["directory"].mkdir()
            (evidence.kwargs["directory"] / "keep").write_bytes(b"other writer")
        return original(**args)
    evidence.client.get_object = race
    with pytest.raises(restore.RestoreError):
        run(evidence)
    assert list(evidence.kwargs["directory"].iterdir()) == [evidence.kwargs["directory"] / "keep"]
    assert (evidence.kwargs["directory"] / "keep").read_bytes() == b"other writer"


def test_local_file_corruption_before_completion_cannot_report_success(evidence, monkeypatch):
    original = restore._write
    def corrupt(root, name, data):
        original(root, name, data)
        if name == ".acceptance-manifest.json":
            (evidence.kwargs["directory"] / "source/course.html").write_bytes(b"disk corruption")
    monkeypatch.setattr(restore, "_write", corrupt)
    with pytest.raises(restore.RestoreError, match="local_integrity"):
        run(evidence)


def test_resource_bounds(evidence, monkeypatch):
    monkeypatch.setattr(restore, "MAX_TOTAL", 1)
    with pytest.raises(restore.RestoreError, match="size_limit"):
        run(evidence)
    assert len(evidence.client.calls) == 1


def test_manifest_byte_limit(evidence, monkeypatch):
    monkeypatch.setattr(restore, "MAX_MANIFEST", 64)
    with pytest.raises(restore.RestoreError, match="integrity"):
        run(evidence)
    assert len(evidence.client.calls) == 1
    assert all(b.closed for b in evidence.client.bodies)
    assert not evidence.kwargs["directory"].exists()


def test_file_count_limit(evidence):
    evidence.manifest["files"] *= 51
    pin(evidence)
    with pytest.raises(restore.RestoreError):
        run(evidence)
    assert len(evidence.client.calls) == 1


def test_conflicting_file_and_directory_paths_rejected(evidence):
    files = evidence.manifest["files"]
    files[1]["path"] = files[0]["path"] + "/course.html"
    base = evidence.kwargs["manifest_key"].removesuffix("manifest.json")
    files[1]["key"] = base + "files/" + files[1]["path"]
    pin(evidence)
    with pytest.raises(restore.RestoreError, match="conflicting"):
        run(evidence)
    assert len(evidence.client.calls) == 1


def test_short_stream_reads_are_fully_consumed_and_closed(evidence):
    original = evidence.client.get_object
    class ShortReader(io.BytesIO):
        def read(self, size=-1):
            return super().read(min(size, 3))
    def short_read(**args):
        response = original(**args)
        body = response["Body"]
        response["Body"] = ShortReader(body.read())
        body.close()
        evidence.client.bodies.append(response["Body"])
        return response
    evidence.client.get_object = short_read
    assert run(evidence)["status"] == "restored"
    assert all(b.closed for b in evidence.client.bodies)


def test_nested_symlink_cannot_redirect_restored_files(evidence, monkeypatch, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    original = restore._write
    def redirect(root, name, data):
        if name == "source/course.html":
            (evidence.kwargs["directory"] / "source").symlink_to(outside, target_is_directory=True)
        original(root, name, data)
    monkeypatch.setattr(restore, "_write", redirect)
    with pytest.raises(restore.RestoreError):
        run(evidence)
    assert list(outside.iterdir()) == []


def test_file_created_during_install_is_never_overwritten(evidence, monkeypatch):
    original = restore._write
    def collide(root, name, data):
        path = evidence.kwargs["directory"] / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"another writer's file")
        original(root, name, data)
    monkeypatch.setattr(restore, "_write", collide)
    with pytest.raises(restore.RestoreError):
        run(evidence)
    assert (evidence.kwargs["directory"] / "initial/receipt.json").read_bytes() == b"another writer's file"


def cli_args(p):
    return ["--directory", str(p.kwargs["directory"]), "--bucket", BUCKET,
            "--expected-owner", OWNER, "--region", REGION,
            "--manifest-key", p.kwargs["manifest_key"],
            "--manifest-version-id", p.kwargs["version_id"],
            "--manifest-sha256", p.kwargs["sha256"]]


@pytest.mark.parametrize("fail", [False, True])
def test_cli_success_and_safe_failure_output(evidence, monkeypatch, capsys, fail):
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=lambda *a, **k: evidence.client))
    if fail:
        def broken(**_args):
            raise RuntimeError("private-provider-details")
        evidence.client.get_object = broken
    result = restore.main(cli_args(evidence))
    output = capsys.readouterr()
    assert result == int(fail) and evidence.client.closed
    assert json.loads(output.out)["status"] == ("not_restored" if fail else "restored")
    assert "private-provider-details" not in output.out + output.err
    assert "new_acceptance" in output.out


def test_local_write_failure_reports_failure_and_preserves_partial_files(evidence, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=lambda *a, **k: evidence.client))
    original = restore._write
    def disk_failure(root, name, data):
        original(root, name, data)
        raise OSError("sensitive local filesystem details")
    monkeypatch.setattr(restore, "_write", disk_failure)
    assert restore.main(cli_args(evidence)) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["status"] == "not_restored"
    assert "sensitive" not in output.out + output.err
    assert (evidence.kwargs["directory"] / "initial/receipt.json").is_file()
    assert not (evidence.kwargs["directory"] / ".acceptance-manifest.json").exists()
    assert evidence.client.closed


def test_archive_output_is_directly_recoverable(tmp_path):
    class UploaderStorage(Storage):
        def get_public_access_block(self, **_args):
            return {"PublicAccessBlockConfiguration": {k: True for k in (
                "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")}}
        def get_bucket_versioning(self, **_args):
            return {"Status": "Enabled"}
        def get_bucket_lifecycle_configuration(self, **_args):
            return {"Rules": []}
        def put_object(self, **args):
            version = f"v{len(self.objects)}"
            self.add(args["Key"], version, args["Body"])
            return {"VersionId": version}
    source = tmp_path / "source"
    source.mkdir()
    data = b"<!doctype html><html>Source</html>"
    (source / "course.html").write_bytes(data)
    client = UploaderStorage()
    saved = archive.archive(client, directory=source, bucket=BUCKET, owner=OWNER,
                            inventory={"schema_version": 1, "acceptance_id": "round-trip",
                                       "files": [{"path": "course.html", "sha256": archive.sha(data)}]})
    proof = saved["manifest"]
    result = restore.restore_evidence(
        client, directory=tmp_path / "restored", bucket=BUCKET, owner=OWNER, region=REGION,
        manifest_key=proof["key"], version_id=proof["version_id"], sha256=proof["sha256"])
    assert result["status"] == "restored"
    assert (tmp_path / "restored/course.html").read_bytes() == data
