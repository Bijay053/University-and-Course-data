"""Offline execution: permission scenarios, role template and ownership fences."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from deploy import archive_permission_scenarios as scenarios
from deploy import rehearse_archive_permissions as harness
from deploy.archive_permission_contract import archive_policy

spec = importlib.util.spec_from_file_location(
    "archive_storage_test_fixtures", Path(__file__).with_name("test_archive_storage_harness.py"))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class Restricted:
    def __init__(self, storage):
        self.storage = storage
        self.denials = []

    def __getattr__(self, name):
        def call(**args):
            allowed = name in {
                "get_public_access_block", "get_bucket_versioning",
                "get_bucket_lifecycle_configuration", "put_object", "get_object",
            } and ("Key" not in args or args["Key"].startswith(scenarios.archive.PREFIX))
            if allowed:
                try:
                    return getattr(self.storage, name)(**args)
                except ClientError as exc:
                    if exc.response["Error"]["Code"] != "NoSuchKey":
                        raise
            self.denials.append(name)
            raise ClientError({"Error": {"Code": "AccessDenied"},
                               "ResponseMetadata": {"HTTPStatusCode": 403}}, name)
        return call


def test_actual_archive_scenarios_use_restricted_client(tmp_path):
    storage = fixtures.S3()
    restricted = Restricted(storage)
    checks = scenarios.exercise(
        restricted, observer=storage, bucket="disposable-contract",
        owner=fixtures.ACCOUNT, directory=tmp_path, owned=lambda: None)
    assert checks == scenarios.CHECKS
    assert restricted.denials.count("delete_object") == 2
    assert {"put_bucket_policy", "delete_bucket_policy", "put_bucket_lifecycle_configuration",
            "delete_bucket_lifecycle"} <= set(restricted.denials)
    assert storage.deleted == []


@pytest.mark.parametrize("code,status", [("NoSuchKey", 404), ("AccessDenied", 400),
                                        ("InvalidRequest", 400)])
def test_denial_requires_aws_access_denied(code, status):
    def operation():
        raise ClientError({"Error": {"Code": code},
                           "ResponseMetadata": {"HTTPStatusCode": status}}, "test")
    with pytest.raises(RuntimeError, match="actual AWS"):
        scenarios.denied(operation)


def test_unexpected_permission_is_a_failure():
    with pytest.raises(RuntimeError, match="unexpectedly"):
        scenarios.denied(lambda: {})


def test_role_has_only_archive_contract_and_exact_trust():
    principal = f"arn:aws:iam::{fixtures.ACCOUNT}:user/test-harness"
    body = harness.permission_template(fixtures.RUN, "disposable-contract", fixtures.ACCOUNT, principal)
    role = body["Resources"]["ArchiveRole"]["Properties"]
    assert role["Policies"] == [{"PolicyName": "archive-only",
                                "PolicyDocument": archive_policy("disposable-contract", fixtures.ACCOUNT)}]
    assert role["AssumeRolePolicyDocument"]["Statement"] == [{
        "Effect": "Allow", "Action": "sts:AssumeRole", "Principal": {"AWS": principal}}]
    assert role["Tags"] == [{"Key": harness.TAG, "Value": fixtures.RUN}]
    assert "ManagedPolicyArns" not in role


@pytest.mark.parametrize("principal", ["*", "arn:aws:iam::999999999999:user/elsewhere",
                                     f"arn:aws:iam::{fixtures.ACCOUNT}:root"])
def test_role_trust_refuses_broad_or_foreign_principals(principal):
    with pytest.raises(RuntimeError):
        harness.permission_template(fixtures.RUN, "disposable-contract", fixtures.ACCOUNT, principal)


def test_cleanup_refuses_role_with_wrong_ownership():
    storage = fixtures.S3()
    cf = fixtures.CF(storage)
    role = {"Arn": f"arn:aws:iam::{fixtures.ACCOUNT}:role/{harness.role_name(fixtures.RUN)}",
            "Tags": [{"Key": harness.TAG, "Value": "another-run"}]}
    resource = harness.PermissionResources(
        iam=SimpleNamespace(get_role=lambda **kw: {"Role": role}),
        sts=SimpleNamespace(get_caller_identity=lambda: {"Account": fixtures.ACCOUNT}),
        s3=storage, cf=cf, account=fixtures.ACCOUNT, production=fixtures.PRODUCTION,
        region=fixtures.REGION, run_id=fixtures.RUN)
    with pytest.raises(RuntimeError, match="role ownership"):
        resource.cleanup()
    assert not cf.deleted and storage.deleted == []


def test_separate_proof_domain(tmp_path):
    kms, arn, pins = fixtures.signing_fixture(tmp_path)
    resources = fixtures.resources()
    resources.state["cleanup_verified"] = True
    output = tmp_path / "proof.json"
    fixtures.proof.write_proof(
        output, kms=kms, key_id=arn, account=fixtures.ACCOUNT, region=fixtures.REGION,
        signers=pins, state=resources.state, checks=scenarios.CHECKS, permissions=True)
    fixtures.proof.validate_proof(output, account=fixtures.ACCOUNT, run_id=fixtures.RUN,
                                 signers=pins, permissions=True)
    with pytest.raises(ValueError, match="mismatch"):
        fixtures.proof.validate_proof(output, account=fixtures.ACCOUNT, run_id=fixtures.RUN,
                                     signers=pins)


def test_approval_required_before_aws_client(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "session", lambda: pytest.fail("AWS called without approval"))
    with pytest.raises(RuntimeError, match="approval"):
        harness.rehearse(account=fixtures.ACCOUNT, production=fixtures.PRODUCTION,
                         region=fixtures.REGION, run_id=fixtures.RUN,
                         proof_output=tmp_path / "proof", state_output=tmp_path / "state",
                         key_id="unused", signers="unused")


@pytest.mark.parametrize("failure", [None, "lost-create", "scenario", "cleanup"])
def test_whole_permission_runner_separates_roles_and_cleans_before_signing(
        tmp_path, monkeypatch, failure):
    storage = fixtures.S3()
    cf = fixtures.CF(storage)
    cf.exists = storage.exists = False
    role = {"exists": False}
    role_arn = f"arn:aws:iam::{fixtures.ACCOUNT}:role/{harness.role_name(fixtures.RUN)}"
    def get_role(**args):
        if not role["exists"]:
            raise fixtures.error("NoSuchEntity")
        return {"Role": {"Arn": role_arn, "Tags": [
            {"Key": harness.TAG, "Value": fixtures.RUN}]}}
    iam = SimpleNamespace(get_role=get_role)
    def create(**args):
        assert args["Capabilities"] == ["CAPABILITY_NAMED_IAM"]
        role["exists"] = cf.exists = storage.exists = True
        if failure == "lost-create":
            raise TimeoutError("lost response")
    cf.create_stack = create
    delete = cf.delete_stack
    def remove(**args):
        delete(**args)
        role["exists"] = False
    cf.delete_stack = remove
    assumed = []
    def assume(**args):
        assert args["RoleArn"] == role_arn
        assert json.loads(args["Policy"]) == archive_policy(
            f"uni-archive-it-{fixtures.ACCOUNT}-{fixtures.RUN}", fixtures.ACCOUNT)
        assumed.append(args)
        return {"Credentials": {"AccessKeyId": "offline", "SecretAccessKey": "offline",
                                "SessionToken": "offline"}}
    sts = SimpleNamespace(
        get_caller_identity=lambda: {"Account": fixtures.ACCOUNT,
            "Arn": f"arn:aws:iam::{fixtures.ACCOUNT}:user/harness"}, assume_role=assume)
    role_sts = SimpleNamespace(get_caller_identity=lambda: {
        "Account": fixtures.ACCOUNT,
        "Arn": f"arn:aws:sts::{fixtures.ACCOUNT}:assumed-role/{harness.role_name(fixtures.RUN)}/test"})
    restricted = Restricted(storage)
    kms, arn, pins = fixtures.signing_fixture(tmp_path)
    original_sign = kms.sign
    def sign(**args):
        assert not cf.exists and not storage.exists and not storage.items and not role["exists"]
        return original_sign(**args)
    kms.sign = sign
    services = {"sts": sts, "iam": iam, "s3": storage, "cloudformation": cf, "kms": kms}
    restricted_services = {"sts": role_sts, "s3": restricted}
    for service in [*services.values(), *restricted_services.values()]:
        service.close = lambda: None
    monkeypatch.setattr(harness, "session", lambda: SimpleNamespace(
        client=lambda name, **kw: services[name]))
    monkeypatch.setattr(harness.boto3, "Session", lambda **kw: SimpleNamespace(
        client=lambda name, **args: restricted_services[name]))
    monkeypatch.setattr(harness.time, "sleep", lambda seconds: None)
    real_exercise = harness.exercise
    def exercise(client, **kwargs):
        assert client is restricted and kwargs["observer"] is storage
        checks = real_exercise(client, **kwargs)
        if failure == "scenario":
            raise RuntimeError("scenario failed")
        return checks
    monkeypatch.setattr(harness, "exercise", exercise)
    original_delete = storage.delete_object
    if failure == "cleanup":
        def refuse(**kwargs):
            raise fixtures.error("AccessDenied")
        storage.delete_object = refuse
    output, state = tmp_path / "proof", tmp_path / "state"
    args = dict(account=fixtures.ACCOUNT, production=fixtures.PRODUCTION,
                region=fixtures.REGION, run_id=fixtures.RUN, proof_output=output,
                state_output=state, key_id=arn, signers=pins, opt_in=True)
    if failure:
        with pytest.raises((RuntimeError, TimeoutError)):
            harness.rehearse(**args)
        assert not output.exists()
        if failure == "cleanup":
            assert json.loads(state.read_text())["cleanup_verified"] is False
            storage.delete_object = original_delete
            harness.rehearse(**args, cleanup_only=True)
    else:
        harness.rehearse(**args)
        fixtures.proof.validate_proof(output, account=fixtures.ACCOUNT,
                                     run_id=fixtures.RUN, signers=pins, permissions=True)
    assert json.loads(state.read_text())["cleanup_verified"] is True
    assert not role["exists"] and not cf.exists and not storage.exists
    assert len(assumed) == (0 if failure == "lost-create" else 1)
