"""Offline runner safety tests. Real IAM evidence is recorded separately."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from deploy import rehearse_acceptance_evidence as harness
from deploy.rehearse_archive_permissions import role_name

spec = importlib.util.spec_from_file_location(
    "acceptance_storage_fixtures", Path(__file__).with_name("test_archive_storage_harness.py"))
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
            } and ("Key" not in args or args["Key"].startswith(harness.archive.PREFIX))
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


def test_policy_has_no_listing_deletion_or_bucket_mutation():
    policy = harness.policy("disposable-contract", fixtures.ACCOUNT)
    actions = {action for statement in policy["Statement"] for action in statement["Action"]}
    assert actions == {
        "s3:GetBucketPublicAccessBlock", "s3:GetBucketVersioning",
        "s3:GetLifecycleConfiguration", "s3:GetObject",
        "s3:GetObjectVersion", "s3:PutObject",
    }
    assert policy["Statement"][1]["Resource"] == (
        "arn:aws:s3:::disposable-contract/fresh-course-acceptance/v1/*")
    assert all(s["Condition"]["StringEquals"]["s3:ResourceAccount"] == fixtures.ACCOUNT
               for s in policy["Statement"])


def test_temporary_grant_is_exact_resource_scoped():
    role = f"arn:aws:iam::{fixtures.ACCOUNT}:role/disposable"
    statements = harness.harness_policy("disposable-contract", role)["Statement"]
    assert {s["Resource"] for s in statements} == {
        "arn:aws:s3:::disposable-contract",
        "arn:aws:s3:::disposable-contract/fresh-course-acceptance/v1/*", role}
    assert all("*" not in s["Action"] for s in statements)


def test_all_scenarios_and_local_retention(tmp_path):
    storage = fixtures.S3()
    resources = fixtures.resources(storage)
    restricted = Restricted(storage)
    result = harness.exercise(restricted, resources, tmp_path)
    assert len(result["checks"]) == 13
    assert result["conditional_conflicts"] == 5
    assert result["exact_version_reads"] == 9
    assert set(restricted.denials) == {
        "list_objects_v2", "get_object", "delete_object", "put_object", "put_bucket_versioning"}
    assert storage.deleted == []
    assert len(storage.items) == 7
    assert len(list(tmp_path.iterdir())) == 2


def test_approval_required_before_any_aws_access(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "session", lambda: pytest.fail("unexpected AWS call"))
    with pytest.raises(RuntimeError, match="approval"):
        harness.run(account=fixtures.ACCOUNT, production=fixtures.PRODUCTION,
                    region=fixtures.REGION, run_id=fixtures.RUN, output=tmp_path)


@pytest.mark.parametrize("failure", [None, "lost-create", "scenario", "cleanup"])
def test_runner_cleanup_and_recovery(tmp_path, monkeypatch, failure):
    storage = fixtures.S3()
    cf = fixtures.CF(storage)
    cf.exists = storage.exists = False
    role = {"exists": False}
    grant = {}
    role_arn = f"arn:aws:iam::{fixtures.ACCOUNT}:role/{role_name(fixtures.RUN)}"
    principal = f"arn:aws:iam::{fixtures.ACCOUNT}:user/harness"

    def get_role(**args):
        if not role["exists"]:
            raise fixtures.error("NoSuchEntity")
        return {"Role": {"Arn": role_arn, "Tags": [
            {"Key": harness.TAG, "Value": fixtures.RUN}]}}

    def create(**args):
        document = json.loads(args["TemplateBody"])
        assert document["Resources"]["ArchiveRole"]["Properties"]["Policies"][0][
            "PolicyDocument"] == harness.policy(
                f"uni-archive-it-{fixtures.ACCOUNT}-{fixtures.RUN}", fixtures.ACCOUNT)
        role["exists"] = cf.exists = storage.exists = True
        if failure == "lost-create":
            raise TimeoutError("lost create response")

    cf.create_stack = create
    delete = cf.delete_stack

    def remove(**args):
        delete(**args)
        role["exists"] = False

    cf.delete_stack = remove

    def put_grant(**args):
        grant[args["PolicyName"]] = args["PolicyDocument"]

    def delete_grant(**args):
        assert not cf.exists and not storage.exists and not role["exists"]
        del grant[args["PolicyName"]]

    iam = SimpleNamespace(
        get_role=get_role, list_user_policies=lambda **kw: {"PolicyNames": list(grant)},
        put_user_policy=put_grant, delete_user_policy=delete_grant)
    sts = SimpleNamespace(
        get_caller_identity=lambda: {"Account": fixtures.ACCOUNT, "Arn": principal},
        assume_role=lambda **kw: {"Credentials": {
            "AccessKeyId": "offline", "SecretAccessKey": "offline", "SessionToken": "offline"}})
    restricted_sts = SimpleNamespace(get_caller_identity=lambda: {
        "Account": fixtures.ACCOUNT,
        "Arn": f"arn:aws:sts::{fixtures.ACCOUNT}:assumed-role/{role_name(fixtures.RUN)}/test"})
    restricted = Restricted(storage)
    services = {"sts": sts, "iam": iam, "s3": storage, "cloudformation": cf}
    restricted_services = {"sts": restricted_sts, "s3": restricted}
    closed = []
    for name, service in [*services.items(), *restricted_services.items()]:
        service.close = lambda name=name: closed.append(name)
    monkeypatch.setattr(harness, "session", lambda: SimpleNamespace(
        client=lambda name, **kw: services[name]))
    monkeypatch.setattr(harness.boto3, "Session", lambda **kw: SimpleNamespace(
        client=lambda name, **args: restricted_services[name]))
    monkeypatch.setattr(harness.time, "sleep", lambda seconds: None)
    real_exercise = harness.exercise

    def exercise(client, resources, directory):
        assert client is restricted and resources.s3 is storage
        result = real_exercise(client, resources, directory)
        if failure == "scenario":
            raise RuntimeError("scenario failure")
        return result

    monkeypatch.setattr(harness, "exercise", exercise)
    original_delete = storage.delete_object
    if failure == "cleanup":
        def refuse(**kw):
            raise fixtures.error("AccessDenied")
        storage.delete_object = refuse
    args = dict(account=fixtures.ACCOUNT, production=fixtures.PRODUCTION,
                region=fixtures.REGION, run_id=fixtures.RUN, output=tmp_path, approve=True)
    proof = tmp_path / f"{fixtures.RUN}-proof.json"
    state = tmp_path / f"{fixtures.RUN}-state.json"
    if failure:
        with pytest.raises((RuntimeError, TimeoutError, ClientError)):
            harness.run(**args)
        assert not proof.exists()
        if failure == "cleanup":
            assert grant
            assert json.loads(state.read_text())["cleanup_verified"] is False
            storage.delete_object = original_delete
            harness.run(**args, cleanup_only=True)
    else:
        harness.run(**args)
        assert json.loads(proof.read_text())["result"] == "passed"
    saved = json.loads(state.read_text())
    assert saved["cleanup_verified"] and saved["temporary_grant_removed"]
    assert not grant and not role["exists"] and not cf.exists and not storage.exists
    assert {"sts", "iam", "s3", "cloudformation"} <= set(closed)
