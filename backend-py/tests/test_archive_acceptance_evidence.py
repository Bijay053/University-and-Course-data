"""Offline acceptance archive contract; no AWS, database or environment access."""
import importlib.util
import io
import json
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

spec = importlib.util.spec_from_file_location(
    "acceptance_archive", Path(__file__).parents[1] / "deploy/archive_acceptance_evidence.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Storage:
    def __init__(self):
        self.objects = {}
        self.writes = []
        self.reads = []
        self.versioning = "Enabled"
        self.public = False
        self.rules = []
        self.corrupt = None
        self.no_version = False

    def get_public_access_block(self, **args):
        return {"PublicAccessBlockConfiguration": {k: not self.public for k in (
            "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")}}

    def get_bucket_versioning(self, **args):
        return {"Status": self.versioning}

    def get_bucket_lifecycle_configuration(self, **args):
        return {"Rules": self.rules}

    def put_object(self, **args):
        assert args["ExpectedBucketOwner"] == "123456789012"
        assert args["IfNoneMatch"] == "*"
        assert args["ServerSideEncryption"] == "AES256"
        key = args["Key"]
        if key in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        version = f"version-{len(self.objects)}"
        self.objects[key] = (args["Body"], args["Metadata"], version)
        self.writes.append(key)
        return {"VersionId": "null" if self.no_version else version}

    def get_object(self, **args):
        data, metadata, version = self.objects[args["Key"]]
        self.reads.append(args)
        if self.corrupt and self.corrupt in args["Key"]:
            data = b"corrupt"
        return {"Body": io.BytesIO(data), "Metadata": metadata, "VersionId": version}


@pytest.fixture
def evidence(tmp_path):
    files = {
        "course.html": b"<!doctype html><html>Official course tuition</html>",
        "receipt.json": module.encode({"started_at": "2026-11-02", "cases": [],
                                      "code_sha256": {}}),
    }
    for path, data in files.items():
        (tmp_path / path).write_bytes(data)
    inventory = {"schema_version": 1, "acceptance_id": "test-acceptance",
                 "files": [{"path": p, "sha256": module.sha(d)} for p, d in files.items()]}
    return tmp_path, inventory


def run(storage, evidence):
    directory, inventory = evidence
    return module.archive(storage, directory=directory, inventory=inventory,
                          bucket="existing-bucket", owner="123456789012")


def test_versioned_manifest_and_idempotent_retry(evidence):
    storage = Storage()
    first = run(storage, evidence)
    assert first["status"] == "retained"
    assert first["file_count"] == 2
    manifest_data = storage.objects[first["manifest"]["key"]][0]
    assert module.sha(manifest_data) == first["manifest"]["sha256"]
    manifest = json.loads(manifest_data)
    assert all(f["version_id"] and f["sha256"] for f in manifest["files"])
    assert all("VersionId" in call for call in storage.reads)
    assert run(storage, evidence) == first
    assert len(storage.writes) == 3
    assert len(list(evidence[0].iterdir())) == 2


@pytest.mark.parametrize("failure", ["public", "suspended", "expiration", "noncurrent"])
def test_storage_preflight_fails_without_writes(evidence, failure):
    storage = Storage()
    if failure == "public":
        storage.public = True
    elif failure == "suspended":
        storage.versioning = "Suspended"
    else:
        field = "Expiration" if failure == "expiration" else "NoncurrentVersionExpiration"
        storage.rules = [{"Status": "Enabled", "Filter": {"Prefix": module.PREFIX},
                          field: {"Days": 30}}]
    with pytest.raises(ValueError):
        run(storage, evidence)
    assert not storage.writes


@pytest.mark.parametrize("target", ["course.html", "manifest.json"])
def test_remote_corruption_never_reports_retained(evidence, target):
    storage = Storage()
    storage.corrupt = target
    with pytest.raises(ValueError, match="integrity"):
        run(storage, evidence)
    assert (evidence[0] / "receipt.json").exists()
    if target == "course.html":
        assert not any(k.endswith("manifest.json") for k in storage.writes)


def test_no_version_fails(evidence):
    storage = Storage()
    storage.no_version = True
    with pytest.raises(ValueError, match="version"):
        run(storage, evidence)


@pytest.mark.parametrize("bad_path", ["../receipt.json", "/receipt.json", ".env",
                                       "server.log", "operational.json", "x/../course.html"])
def test_path_allowlist(evidence, bad_path):
    evidence[1]["files"][0]["path"] = bad_path
    storage = Storage()
    with pytest.raises(ValueError):
        run(storage, evidence)
    assert not storage.writes


def test_unlisted_logs_are_never_read(evidence):
    (evidence[0] / ".env").write_text("password=do-not-upload")
    (evidence[0] / "worker.log").write_text("private log")
    storage = Storage()
    run(storage, evidence)
    assert not any("worker" in key or ".env" in key for key in storage.writes)


def test_hash_mismatch_fails_before_first_upload(evidence):
    (evidence[0] / "receipt.json").write_text("{}")
    storage = Storage()
    with pytest.raises(ValueError, match="hash"):
        run(storage, evidence)
    assert not storage.writes


def test_symlink_rejected(evidence):
    (evidence[0] / "course.html").unlink()
    (evidence[0] / "course.html").symlink_to(evidence[0] / "receipt.json")
    with pytest.raises(ValueError, match="symlink"):
        run(Storage(), evidence)


@pytest.mark.parametrize("payload", [
    {"environment": {"TOKEN": "example"}},
    {"stdout": "raw operational log"},
    {"cases": [{"credentials": "example"}]},
])
def test_receipt_secrets_logs_and_environment_rejected(evidence, payload):
    data = module.encode({"started_at": "today", "cases": [], "code_sha256": {}, **payload})
    (evidence[0] / "receipt.json").write_bytes(data)
    evidence[1]["files"][1]["sha256"] = module.sha(data)
    storage = Storage()
    with pytest.raises(ValueError):
        run(storage, evidence)
    assert not storage.writes


def test_html_secret_rejected(evidence):
    data = b"<html>api_key=private-secret-value</html>"
    (evidence[0] / "course.html").write_bytes(data)
    evidence[1]["files"][0]["sha256"] = module.sha(data)
    with pytest.raises(ValueError, match="sensitive"):
        run(Storage(), evidence)


def test_public_form_labels_are_not_credentials(evidence):
    data = b'<html>{"password":"password","recaptchaMissingCookie":"Please enable cookies"}</html>'
    (evidence[0] / "course.html").write_bytes(data)
    evidence[1]["files"][0]["sha256"] = module.sha(data)
    assert run(Storage(), evidence)["status"] == "retained"


def test_explicit_opt_in_and_safe_failure_output(evidence, capsys):
    assert module.main(["--directory", str(evidence[0]), "--inventory", "missing",
                        "--bucket", "existing", "--expected-owner", "123456789012",
                        "--region", "eu-west-2"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "not_retained"


def test_version_readback_must_match():
    storage = Storage()
    args = {"Bucket": "existing", "ExpectedBucketOwner": "123456789012", "Key": "test"}
    proof = module.store(storage, args, b"test", "text/html")
    with pytest.raises(ValueError):
        module.verify(storage, {**args, "VersionId": "different"}, b"test")
    assert proof["version_id"]
