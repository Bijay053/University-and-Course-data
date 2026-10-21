"""Opt-in disposable S3 archive rehearsal. No application, SSM or scrape imports."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .archive_storage_proof import (
    PRODUCTION_ACCOUNT_IDS, atomic_json, pinned_key, write_proof,
)
from .archive_storage_scenarios import exercise, require, versions

TAG = "university-portal:disposable-archive"
FAILURES = ("after-stack-creation", "after-upload")


def session():
    # Never fall back to the workspace/host's production credential chain.
    required = ("DISPOSABLE_AWS_ACCESS_KEY_ID", "DISPOSABLE_AWS_SECRET_ACCESS_KEY")
    if not all(os.environ.get(name) for name in required):
        raise ValueError("explicit disposable AWS credentials required")
    return boto3.Session(
        aws_access_key_id=os.environ[required[0]],
        aws_secret_access_key=os.environ[required[1]],
        aws_session_token=os.environ.get("DISPOSABLE_AWS_SESSION_TOKEN") or None,
    )


def account_guard(sts, account, production):
    require(bool(re.fullmatch(r"\d{12}", account)) and
            bool(re.fullmatch(r"\d{12}", production)) and
            account != production and account not in PRODUCTION_ACCOUNT_IDS,
            "explicit non-production account required")
    require(sts.get_caller_identity()["Account"] == account, "AWS account mismatch")


def template(run_id, bucket):
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Resources": {"ArchiveBucket": {
            "Type": "AWS::S3::Bucket",
            "Properties": {
                "BucketName": bucket,
                "Tags": [{"Key": TAG, "Value": run_id}],
                "VersioningConfiguration": {"Status": "Enabled"},
                "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]},
                "PublicAccessBlockConfiguration": {
                    key: True for key in (
                        "BlockPublicAcls", "IgnorePublicAcls",
                        "BlockPublicPolicy", "RestrictPublicBuckets",
                    )
                },
                "BucketEncryption": {"ServerSideEncryptionConfiguration": [
                    {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}},
                ]},
            },
        }},
    }


class Resources:
    def __init__(self, *, sts, s3, cf, account, production, region, run_id):
        require(bool(re.fullmatch(r"[a-f0-9]{16}", run_id)), "invalid run ID")
        self.sts, self.s3, self.cf = sts, s3, cf
        self.account, self.production = account, production
        self.state = {
            "run_id": run_id, "account_id": account, "region": region,
            "bucket": f"uni-archive-it-{account}-{run_id}",
            "stack_name": f"uni-archive-it-{run_id}",
            "cleanup_verified": False,
        }
        self.args = {"Bucket": self.state["bucket"], "ExpectedBucketOwner": account}

    def account_guard(self):
        account_guard(self.sts, self.account, self.production)

    def stack(self):
        try:
            return self.cf.describe_stacks(StackName=self.state["stack_name"])["Stacks"][0]
        except ClientError as exc:
            # ValidationError can mean other things. Never treat arbitrary errors as absence.
            if (exc.response["Error"]["Code"] == "ValidationError"
                    and "does not exist" in exc.response["Error"].get("Message", "")):
                return None
            raise

    def owned_stack(self):
        self.account_guard()
        stack = self.stack()
        if stack is not None:
            require({t["Key"]: t["Value"] for t in stack.get("Tags", [])}.get(TAG)
                    == self.state["run_id"], "stack ownership mismatch")
        return stack

    def owned(self):
        require(self.owned_stack() is not None, "owned stack missing")
        tags = self.s3.get_bucket_tagging(**self.args)["TagSet"]
        require({t["Key"]: t["Value"] for t in tags}.get(TAG) == self.state["run_id"],
                "bucket ownership mismatch")

    def bucket_absent(self):
        try:
            self.s3.head_bucket(**self.args)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"404", "NoSuchBucket"}:
                return True
            raise
        return False

    def assert_absent(self):
        self.account_guard()
        require(self.stack() is None, "disposable stack remains")
        require(self.bucket_absent(), "disposable bucket remains")

    def cleanup(self):
        # Creation may have succeeded even if the caller lost the response.
        for _ in range(120):
            stack = self.owned_stack()
            if stack is None or not stack["StackStatus"].endswith("_IN_PROGRESS"):
                break
            time.sleep(5)
        else:
            raise RuntimeError("stack did not settle; use saved state to retry cleanup")
        if not self.bucket_absent():
            self.owned()
            # Include delete markers and use exact versions, never unversioned deletes.
            for item in versions(self.s3, self.args):
                self.owned()
                self.s3.delete_object(**self.args, Key=item["Key"], VersionId=item["VersionId"])
            require(versions(self.s3, self.args) == [], "object versions remain after cleanup")
        if self.owned_stack() is not None:
            self.cf.delete_stack(StackName=self.state["stack_name"])
            self.cf.get_waiter("stack_delete_complete").wait(
                StackName=self.state["stack_name"], WaiterConfig={"Delay": 5, "MaxAttempts": 120},
            )
        self.assert_absent()
        self.state["cleanup_verified"] = True


def preflight_outputs(*paths):
    for path in paths:
        require(not path.exists() and not path.is_symlink(), "output must be a fresh path")
        require(path.parent.is_dir(), "output parent directory must exist")
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        os.close(fd)
        os.unlink(temporary)
    require(len({p.resolve() for p in paths}) == len(paths), "output paths must differ")


def rehearse(*, account, production, region, run_id, proof_output, state_output,
             key_id, signers, opt_in=False, fail_at=None, cleanup_only=False):
    require(opt_in, "explicit billable disposable AWS approval required")
    require(fail_at is None or fail_at in FAILURES, "invalid failure checkpoint")
    require(bool(re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-\d+", region)), "invalid region")
    proof_output, state_output = Path(proof_output), Path(state_output)
    if not cleanup_only:
        preflight_outputs(proof_output, state_output)
    with ExitStack() as clients:
        sdk = session()
        config = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 2},
                        ignore_configured_endpoint_urls=True)
        def client(name):
            return clients.enter_context(closing(sdk.client(name, region_name=region, config=config)))
        resources = Resources(sts=client("sts"), s3=client("s3"), cf=client("cloudformation"),
                              account=account, production=production, region=region, run_id=run_id)
        resources.account_guard()
        if cleanup_only:
            saved = json.loads(state_output.read_text())
            require(all(saved.get(k) == resources.state[k] for k in
                        ("run_id", "account_id", "region", "bucket", "stack_name")),
                    "cleanup state identity mismatch")
            resources.cleanup()
            atomic_json(state_output, resources.state)
            return
        kms = client("kms")
        pinned_key(kms, key_id, account, region, signers)
        # Reject all pre-existing resources, even if an old run reused this identity.
        resources.assert_absent()
        atomic_json(state_output, resources.state)
        def checkpoint(name):
            resources.state["reached_checkpoint"] = name
            atomic_json(state_output, resources.state)
            if fail_at == name:
                raise RuntimeError(f"injected archive failure at {name}")
        try:
            resources.account_guard()
            resources.cf.create_stack(
                StackName=resources.state["stack_name"],
                TemplateBody=json.dumps(template(run_id, resources.state["bucket"])),
                Tags=[{"Key": TAG, "Value": run_id}],
                ClientRequestToken=f"archive-{run_id}",
                OnFailure="DO_NOTHING",
            )
            resources.cf.get_waiter("stack_create_complete").wait(
                StackName=resources.state["stack_name"], WaiterConfig={"Delay": 5, "MaxAttempts": 120},
            )
            resources.owned()
            checkpoint("after-stack-creation")
            # AWS recommends waiting 15 min after *first* enabling S3 versioning.
            # Do not mistake initial propagation errors for archive failures.
            time.sleep(900)
            with tempfile.TemporaryDirectory(prefix="archive-synthetic-") as directory:
                checks = exercise(resources.s3, bucket=resources.state["bucket"], owner=account,
                                  directory=directory, owned=resources.owned, checkpoint=checkpoint)
        finally:
            try:
                resources.cleanup()
            except Exception:
                resources.state["cleanup_verified"] = False
                atomic_json(state_output, resources.state)
                raise RuntimeError("archive cleanup failed; retry using saved state") from None
            atomic_json(state_output, resources.state)
        resources.account_guard()
        write_proof(proof_output, kms=kms, key_id=key_id, account=account, region=region,
                    signers=signers, state=resources.state, checks=checks)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-account-id", required=True)
    parser.add_argument("--production-account-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--run-id", default=uuid.uuid4().hex[:16])
    parser.add_argument("--proof-output", type=Path, required=True)
    parser.add_argument("--state-output", type=Path, required=True)
    parser.add_argument("--proof-signing-key-id", required=True)
    parser.add_argument("--signers", type=Path, required=True)
    parser.add_argument("--fail-at", choices=FAILURES)
    parser.add_argument("--cleanup-only", action="store_true")
    parser.add_argument("--i-understand-this-creates-disposable-aws-resources", action="store_true")
    args = parser.parse_args(argv)
    try:
        rehearse(account=args.expected_account_id, production=args.production_account_id,
                 region=args.region, run_id=args.run_id, proof_output=args.proof_output,
                 state_output=args.state_output, key_id=args.proof_signing_key_id,
                 signers=args.signers, fail_at=args.fail_at, cleanup_only=args.cleanup_only,
                 opt_in=args.i_understand_this_creates_disposable_aws_resources)
    except Exception:
        # Provider failures can contain credentials; output only a safe recovery message.
        print(json.dumps({"result": "failed", "fail_at": args.fail_at,
                          "action": "inspect cleanup state; retry cleanup if unverified; no success proof"}))
        return 1
    print(json.dumps({"result": "cleanup_verified" if args.cleanup_only else "passed",
                      "proof_output": None if args.cleanup_only else str(args.proof_output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
