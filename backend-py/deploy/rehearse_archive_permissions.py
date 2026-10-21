"""Approved disposable role test. Never use or modify production identities."""
import argparse
from contextlib import ExitStack, closing
import json
from pathlib import Path
import tempfile
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .archive_permission_contract import archive_policy
from .archive_permission_scenarios import CHECKS, exercise
from .archive_storage_proof import atomic_json, pinned_key, write_proof
from .archive_storage_scenarios import require
from .rehearse_archive_storage import (
    Resources, TAG, account_guard, preflight_outputs, session, template,
)


def role_name(run_id):
    return f"uni-archive-reader-{run_id}"


def permission_template(run_id, bucket, account, principal):
    require(principal.startswith(f"arn:aws:iam::{account}:user/")
            or principal.startswith(f"arn:aws:iam::{account}:role/"),
            "explicit disposable IAM principal required")
    result = template(run_id, bucket)
    result["Resources"]["ArchiveRole"] = {
        "Type": "AWS::IAM::Role",
        "Properties": {
            "RoleName": role_name(run_id),
            "Tags": [{"Key": TAG, "Value": run_id}],
            "AssumeRolePolicyDocument": {
                "Version": "2012-10-17", "Statement": [{
                    "Effect": "Allow", "Action": "sts:AssumeRole",
                    "Principal": {"AWS": principal},
                }],
            },
            "Policies": [{"PolicyName": "archive-only",
                          "PolicyDocument": archive_policy(bucket, account)}],
        },
    }
    return result


class PermissionResources(Resources):
    def __init__(self, *, iam, **kwargs):
        super().__init__(**kwargs)
        self.iam = iam
        self.state["role_name"] = role_name(self.state["run_id"])

    def role(self):
        self.account_guard()
        try:
            return self.iam.get_role(RoleName=self.state["role_name"])["Role"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "NoSuchEntity":
                raise
            return None

    def owned_role(self):
        role = self.role()
        if role:
            require(role["Arn"] == f"arn:aws:iam::{self.account}:role/{self.state['role_name']}"
                    and {t["Key"]: t["Value"] for t in role.get("Tags", [])}.get(TAG)
                    == self.state["run_id"], "role ownership mismatch")
        return role

    def owned_stack(self):
        stack = super().owned_stack()
        self.owned_role()
        return stack

    def assert_absent(self):
        super().assert_absent()
        require(self.role() is None, "disposable archive role remains")


def assumed_session(resources, config, clients):
    resources.owned()
    role = resources.owned_role()
    require(role is not None, "archive role missing")
    # IAM is eventually consistent. Retry only its propagation AccessDenied.
    for attempt in range(12):
        resources.account_guard()
        try:
            response = resources.sts.assume_role(
                RoleArn=role["Arn"], RoleSessionName=f"archive-{resources.state['run_id']}",
                DurationSeconds=3600,
                Policy=json.dumps(archive_policy(resources.state["bucket"], resources.account)),
            )
            break
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "AccessDenied" or attempt == 11:
                raise
            time.sleep(5)
    credentials = response["Credentials"]
    sdk = boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
    )
    sts = clients.enter_context(closing(sdk.client(
        "sts", region_name=resources.state["region"], config=config)))
    account_guard(sts, resources.account, resources.production)
    identity = sts.get_caller_identity()
    require(identity["Arn"].startswith(
        f"arn:aws:sts::{resources.account}:assumed-role/{resources.state['role_name']}/"),
        "archive did not assume its restricted role")
    return clients.enter_context(closing(sdk.client(
        "s3", region_name=resources.state["region"], config=config)))


def rehearse(*, account, production, region, run_id, proof_output, state_output,
             key_id, signers, opt_in=False, cleanup_only=False):
    require(opt_in, "separate disposable AWS approval required")
    proof_output, state_output = Path(proof_output), Path(state_output)
    if not cleanup_only:
        preflight_outputs(proof_output, state_output)
    config = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 2},
                    ignore_configured_endpoint_urls=True)
    with ExitStack() as clients:
        sdk = session()
        def client(name):
            return clients.enter_context(closing(sdk.client(name, region_name=region, config=config)))
        resources = PermissionResources(
            sts=client("sts"), s3=client("s3"), cf=client("cloudformation"), iam=client("iam"),
            account=account, production=production, region=region, run_id=run_id)
        resources.account_guard()
        if cleanup_only:
            saved = json.loads(state_output.read_text())
            require(all(saved.get(k) == resources.state[k] for k in (
                "run_id", "account_id", "region", "bucket", "stack_name", "role_name")),
                "cleanup state identity mismatch")
            resources.cleanup()
            atomic_json(state_output, resources.state)
            return
        kms = client("kms")
        pinned_key(kms, key_id, account, region, signers)
        resources.assert_absent()
        principal = resources.sts.get_caller_identity()["Arn"]
        body = permission_template(run_id, resources.state["bucket"], account, principal)
        atomic_json(state_output, resources.state)
        try:
            resources.account_guard()
            resources.cf.create_stack(
                StackName=resources.state["stack_name"], TemplateBody=json.dumps(body),
                Capabilities=["CAPABILITY_NAMED_IAM"], Tags=[{"Key": TAG, "Value": run_id}],
                ClientRequestToken=f"archive-permissions-{run_id}", OnFailure="DO_NOTHING")
            resources.cf.get_waiter("stack_create_complete").wait(
                StackName=resources.state["stack_name"],
                WaiterConfig={"Delay": 5, "MaxAttempts": 120})
            resources.owned()
            # S3 recommends 15 minutes after first enabling bucket versioning.
            time.sleep(900)
            restricted = assumed_session(resources, config, clients)
            with tempfile.TemporaryDirectory(prefix="archive-permissions-") as directory:
                checks = exercise(restricted, observer=resources.s3, bucket=resources.state["bucket"],
                                  owner=account, directory=directory, owned=resources.owned)
            require(checks == CHECKS, "permission checks incomplete")
        finally:
            try:
                resources.cleanup()
            except Exception:
                resources.state["cleanup_verified"] = False
                atomic_json(state_output, resources.state)
                raise RuntimeError("cleanup failed; retry using saved state") from None
            atomic_json(state_output, resources.state)
        write_proof(proof_output, kms=kms, key_id=key_id, account=account, region=region,
                    signers=signers, state=resources.state, checks=checks, permissions=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("expected-account-id", "production-account-id", "region",
                 "proof-output", "state-output", "proof-signing-key-id", "signers"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--run-id", default=uuid.uuid4().hex[:16])
    parser.add_argument("--cleanup-only", action="store_true")
    parser.add_argument("--i-understand-this-creates-disposable-aws-resources", action="store_true")
    args = parser.parse_args(argv)
    try:
        rehearse(account=args.expected_account_id, production=args.production_account_id,
                 region=args.region, run_id=args.run_id, proof_output=args.proof_output,
                 state_output=args.state_output, key_id=args.proof_signing_key_id,
                 signers=args.signers, cleanup_only=args.cleanup_only,
                 opt_in=args.i_understand_this_creates_disposable_aws_resources)
    except Exception as exc:
        # Never print provider messages or temporary credentials.
        code = exc.response["Error"]["Code"] if isinstance(exc, ClientError) else type(exc).__name__
        print(json.dumps({"result": "failed", "error_type": code,
                          "action": "check saved cleanup state; no success proof"}))
        return 1
    print(json.dumps({"result": "cleanup_verified" if args.cleanup_only else "passed"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
