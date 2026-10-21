"""Offline execution of the storage harness; no AWS credentials or application imports."""
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from deploy import archive_storage_proof as proof
from deploy import archive_storage_scenarios as scenarios
from deploy import rehearse_archive_storage as harness

ACCOUNT = "123456789012"
PRODUCTION = "111111111111"
RUN = "a" * 16
REGION = "us-east-1"


def error(code, message="fixture"):
    return ClientError({"Error": {"Code": code, "Message": message}}, "test")


class S3:
    def __init__(self):
        self.items = []
        self.block = {key: True for key in (
            "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets",
        )}
        self.rules = []
        self.tags = [{"Key": harness.TAG, "Value": RUN}]
        self.deleted = []
        self.exists = True
        self.serial = 0

    def get_public_access_block(self, **args):
        return {"PublicAccessBlockConfiguration": self.block}

    def put_public_access_block(self, **args):
        self.block = args["PublicAccessBlockConfiguration"]

    def get_bucket_versioning(self, **args):
        return {"Status": "Enabled"}

    def get_bucket_lifecycle_configuration(self, **args):
        return {"Rules": self.rules}

    def put_bucket_lifecycle_configuration(self, **args):
        self.rules = args["LifecycleConfiguration"]["Rules"]

    def delete_bucket_lifecycle(self, **args):
        self.rules = []

    def put_object(self, **args):
        if args.get("IfNoneMatch") == "*" and any(i["Key"] == args["Key"] for i in self.items):
            raise error("PreconditionFailed")
        self.serial += 1
        item = {**args, "VersionId": str(self.serial)}
        self.items.append(item)
        return {"VersionId": item["VersionId"]}

    def get_object(self, **args):
        for item in reversed(self.items):
            if item["Key"] == args["Key"] and args.get("VersionId", item["VersionId"]) == item["VersionId"]:
                return {**item, "Body": io.BytesIO(item["Body"])}
        raise error("NoSuchKey")

    def get_paginator(self, operation):
        assert operation == "list_object_versions"
        def paginate(**args):
            # Deliberately one item per page to catch first-page-only cleanup.
            for item in self.items:
                if item["Key"].startswith(args.get("Prefix", "")):
                    yield {"DeleteMarkers" if item.get("marker") else "Versions": [
                        {k: item[k] for k in ("Key", "VersionId")},
                    ]}
        return SimpleNamespace(paginate=paginate)

    def get_bucket_tagging(self, **args):
        return {"TagSet": self.tags}

    def head_bucket(self, **args):
        if not self.exists:
            raise error("404")

    def delete_object(self, **args):
        assert args["ExpectedBucketOwner"] == ACCOUNT
        assert args["VersionId"]  # never create a delete marker
        self.deleted.append((args["Key"], args["VersionId"]))
        self.items = [i for i in self.items if (i["Key"], i["VersionId"]) != self.deleted[-1]]


class CF:
    def __init__(self, s3):
        self.s3 = s3
        self.exists = True
        self.tags = [{"Key": harness.TAG, "Value": RUN}]
        self.deleted = False

    def describe_stacks(self, **args):
        if not self.exists:
            raise error("ValidationError", "Stack does not exist")
        return {"Stacks": [{"StackStatus": "CREATE_COMPLETE", "Tags": self.tags}]}

    def delete_stack(self, **args):
        assert self.s3.items == []
        self.deleted = True
        self.exists = self.s3.exists = False

    def get_waiter(self, name):
        return SimpleNamespace(wait=lambda **args: None)


def resources(s3=None):
    s3 = s3 or S3()
    return harness.Resources(
        sts=SimpleNamespace(get_caller_identity=lambda: {"Account": ACCOUNT}),
        s3=s3, cf=CF(s3), account=ACCOUNT, production=PRODUCTION, region=REGION, run_id=RUN,
    )


def test_all_scenarios_execute_without_application_or_sample(tmp_path):
    s3 = S3()
    checkpoints = []
    assert scenarios.exercise(s3, bucket="fixture", owner=ACCOUNT, directory=tmp_path,
                              owned=lambda: None, checkpoint=checkpoints.append) == proof.CHECKS
    assert checkpoints == ["after-upload"]
    assert all(s3.block.values()) and s3.rules == []
    assert s3.items


@pytest.mark.parametrize("account,production,actual", [
    (ACCOUNT, ACCOUNT, ACCOUNT), ("905043442097", PRODUCTION, "905043442097"),
    (ACCOUNT, PRODUCTION, "222222222222"), ("", PRODUCTION, ACCOUNT),
    (ACCOUNT, "", ACCOUNT),
])
def test_account_guard_fails_closed(account, production, actual):
    with pytest.raises(RuntimeError):
        harness.account_guard(SimpleNamespace(get_caller_identity=lambda: {"Account": actual}),
                              account, production)


def test_explicit_opt_in_before_any_sdk_session(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "session", lambda: pytest.fail("created SDK session"))
    with pytest.raises(RuntimeError, match="approval"):
        harness.rehearse(account=ACCOUNT, production=PRODUCTION, region=REGION, run_id=RUN,
                         proof_output=tmp_path / "proof", state_output=tmp_path / "state",
                         key_id="key", signers=tmp_path / "pins")


def test_no_fallback_to_default_credentials(monkeypatch):
    monkeypatch.delenv("DISPOSABLE_AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("DISPOSABLE_AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setattr(harness.boto3, "Session", lambda **kwargs: pytest.fail("SDK invoked"))
    with pytest.raises(ValueError, match="explicit"):
        harness.session()


@pytest.mark.parametrize("wrong", ["stack", "bucket", "account"])
def test_cleanup_refuses_unowned_resources(wrong):
    owned = resources()
    if wrong == "stack":
        owned.cf.tags = []
    elif wrong == "bucket":
        owned.s3.tags = []
    else:
        owned.sts.get_caller_identity = lambda: {"Account": "222222222222"}
    with pytest.raises(RuntimeError):
        owned.cleanup()
    assert not owned.cf.deleted and not owned.s3.deleted


def test_cleanup_paginates_versions_and_markers_and_verifies_absence():
    owned = resources()
    owned.s3.items = [
        {"Key": "same", "VersionId": "one"},
        {"Key": "same", "VersionId": "two"},
        {"Key": "same", "VersionId": "marker", "marker": True},
    ]
    owned.cleanup()
    assert owned.s3.deleted == [("same", "one"), ("same", "two"), ("same", "marker")]
    assert owned.state["cleanup_verified"] is True
    owned.assert_absent()


def test_cleanup_failure_never_reports_verified(monkeypatch):
    owned = resources()
    owned.s3.items = [{"Key": "receipt", "VersionId": "one"}]
    monkeypatch.setattr(owned.s3, "delete_object", lambda **args: None)
    with pytest.raises(RuntimeError, match="versions remain"):
        owned.cleanup()
    assert owned.state["cleanup_verified"] is False and not owned.cf.deleted


def test_forbidden_bucket_is_not_absent(monkeypatch):
    owned = resources()
    def forbidden(**args):
        raise error("403")
    monkeypatch.setattr(owned.s3, "head_bucket", forbidden)
    with pytest.raises(ClientError):
        owned.bucket_absent()


def test_output_preflight_rejects_existing_and_alias_paths(tmp_path):
    path = tmp_path / "proof"
    with pytest.raises(RuntimeError, match="differ"):
        harness.preflight_outputs(path, path)
    path.write_text("previous evidence")
    with pytest.raises(RuntimeError, match="fresh"):
        harness.preflight_outputs(path)
    assert path.read_text() == "previous evidence"


def signing_fixture(tmp_path):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    arn = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/fixture"
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    pins = tmp_path / "signers.json"
    pins.write_text(json.dumps({"schema_version": 1, "signers": [{
        "account_id": ACCOUNT, "signing_key_arn": arn, "public_key_pem": public,
    }]}))
    kms = SimpleNamespace(
        describe_key=lambda **args: {"KeyMetadata": {
            "Arn": arn, "KeyUsage": "SIGN_VERIFY", "KeySpec": "RSA_2048", "Enabled": True,
        }},
        sign=lambda **args: {"Signature": private.sign(
            args["Message"], padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                        salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256(),
        )},
    )
    return kms, arn, pins


def test_signed_proof_pinned_domain_and_tamper_checks(tmp_path):
    kms, arn, pins = signing_fixture(tmp_path)
    state = {**resources().state, "cleanup_verified": True}
    path = tmp_path / "proof.json"
    payload = proof.write_proof(path, kms=kms, key_id=arn, account=ACCOUNT, region=REGION,
                                signers=pins, state=state, checks=proof.CHECKS)
    assert path.stat().st_mode & 0o777 == 0o600
    proof.validate_proof(path, account=ACCOUNT, run_id=RUN, signers=pins)
    payload["bucket"] = "tampered"
    path.write_text(json.dumps(payload))
    with pytest.raises(Exception):
        proof.validate_proof(path, account=ACCOUNT, run_id=RUN, signers=pins)
    payload["kind"] = "ordinary_smoke_acceptance"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="identity"):
        proof.validate_proof(path, account=ACCOUNT, run_id=RUN, signers=pins)


def test_no_proof_without_all_checks_and_cleanup(tmp_path):
    for state, checks in [(resources().state, proof.CHECKS),
                          ({**resources().state, "cleanup_verified": True}, ())]:
        with pytest.raises(ValueError, match="cleanup"):
            proof.write_proof(tmp_path / "proof", kms=None, key_id="", account=ACCOUNT,
                              region=REGION, signers=None, state=state, checks=checks)
    assert not (tmp_path / "proof").exists()


@pytest.mark.parametrize("failure", [None, "after-stack-creation", "after-upload",
                                   "lost-create-response", "cleanup-failure"])
def test_whole_runner_proves_cleanup_before_signing(tmp_path, monkeypatch, failure):
    s3 = S3()
    cf = CF(s3)
    cf.exists = s3.exists = False
    kms, arn, pins = signing_fixture(tmp_path)
    sts = SimpleNamespace(get_caller_identity=lambda: {"Account": ACCOUNT})
    def create(**args):
        assert args["Tags"] == [{"Key": harness.TAG, "Value": RUN}]
        assert json.loads(args["TemplateBody"])["Resources"]["ArchiveBucket"]["Properties"][
            "VersioningConfiguration"] == {"Status": "Enabled"}
        cf.exists = s3.exists = True
        if failure == "lost-create-response":
            raise TimeoutError("created before connection loss")
    cf.create_stack = create
    if failure == "cleanup-failure":
        def refuse(**args):
            raise error("AccessDenied")
        s3.delete_object = refuse
    services = {"s3": s3, "cloudformation": cf, "kms": kms, "sts": sts}
    closed = []
    for name, service in services.items():
        service.close = lambda name=name: closed.append(name)
    monkeypatch.setattr(harness, "session", lambda: SimpleNamespace(
        client=lambda name, **kwargs: services[name],
    ))
    monkeypatch.setattr(harness.time, "sleep", lambda seconds: None)
    original_sign = kms.sign
    def sign(**args):
        assert not cf.exists and not s3.exists and not s3.items
        return original_sign(**args)
    kms.sign = sign
    output = tmp_path / "proof.json"
    state_path = tmp_path / "state.json"
    kwargs = dict(account=ACCOUNT, production=PRODUCTION, region=REGION, run_id=RUN,
                  proof_output=output, state_output=state_path, key_id=arn,
                  signers=pins, opt_in=True,
                  fail_at=failure if failure in harness.FAILURES else None)
    if failure:
        with pytest.raises((RuntimeError, TimeoutError)):
            harness.rehearse(**kwargs)
        assert not output.exists()
    else:
        harness.rehearse(**kwargs)
        proof.validate_proof(output, account=ACCOUNT, run_id=RUN, signers=pins)
    state = json.loads(state_path.read_text())
    assert sorted(closed) == sorted(services)
    if failure == "cleanup-failure":
        assert not state["cleanup_verified"] and cf.exists and s3.exists
        s3.delete_object = S3.delete_object.__get__(s3)
        harness.rehearse(**kwargs, cleanup_only=True)
        assert json.loads(state_path.read_text())["cleanup_verified"]
        assert not output.exists()  # Cleanup-only is never a success attestation.
    else:
        assert state["cleanup_verified"] and not cf.exists and not s3.exists


def test_signer_is_pinned_before_provisioning(tmp_path, monkeypatch):
    kms, arn, pins = signing_fixture(tmp_path)
    pins.write_text('{"schema_version":1,"signers":[]}')
    with pytest.raises(RuntimeError, match="not pinned"):
        proof.pinned_key(kms, arn, ACCOUNT, REGION, pins)


def test_installed_sdk_supports_harness_requests():
    import botocore.session
    sdk = botocore.session.get_session()
    for service, operation, expected in [
        ("s3", "DeleteObject", {"VersionId", "ExpectedBucketOwner"}),
        ("s3", "ListObjectVersions", {"ExpectedBucketOwner"}),
        ("cloudformation", "CreateStack", {"ClientRequestToken", "OnFailure", "Tags"}),
        ("kms", "Sign", {"KeyId", "Message", "MessageType", "SigningAlgorithm"}),
    ]:
        shape = sdk.get_service_model(service).operation_model(operation).input_shape
        assert expected <= set(shape.members)


def test_live_test_driver_recovers_killed_child_offline(tmp_path, monkeypatch):
    import importlib.util
    import subprocess
    spec = importlib.util.spec_from_file_location(
        "archive_storage_integration_driver",
        Path(__file__).with_name("test_archive_storage_aws_integration.py"),
    )
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    settings = {
        "DISPOSABLE_AWS_ACCESS_KEY_ID": "synthetic-not-a-credential",
        "DISPOSABLE_AWS_SECRET_ACCESS_KEY": "synthetic-not-a-credential",
        "DISPOSABLE_AWS_ACCOUNT_ID": ACCOUNT, "PRODUCTION_AWS_ACCOUNT_ID": PRODUCTION,
        "DISPOSABLE_AWS_REGION": REGION, "DISPOSABLE_AWS_PROOF_SIGNING_KEY_ID": "fixture",
        "ARCHIVE_STORAGE_SIGNERS_PATH": str(tmp_path / "pins"),
        "ARCHIVE_STORAGE_PROOF_DIRECTORY": str(tmp_path),
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    owned = resources()
    owned.cf.exists = owned.s3.exists = False
    services = {"sts": owned.sts, "s3": owned.s3, "cloudformation": owned.cf}
    closed = []
    for name, service in services.items():
        service.close = lambda name=name: closed.append(name)
    monkeypatch.setattr(driver, "session", lambda: SimpleNamespace(
        client=lambda name, **kwargs: services[name],
    ))
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        state = Path(command[command.index("--state-output") + 1])
        if "--cleanup-only" not in command:
            state.write_text('{"cleanup_verified":false}')
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        state.write_text('{"cleanup_verified":true}')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(driver.subprocess, "run", run)
    with pytest.raises(AssertionError, match="timed out; ownership-checked cleanup verified"):
        driver.test_archive_storage_and_independent_cleanup(None)
    assert len(calls) == 2 and "--cleanup-only" in calls[-1]
    assert sorted(closed) == sorted(services)
