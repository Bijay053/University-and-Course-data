"""Opt-in live acceptance archive test; synthetic evidence, disposable AWS only.

The operator must separately approve the exact-resource temporary harness grant.
No production credentials, buckets, policies, or application services are used.
"""
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import tempfile
import time
import uuid

from botocore.config import Config
from botocore.exceptions import ClientError, ConnectionClosedError
import boto3

from . import archive_acceptance_evidence as archive
from .archive_permission_contract import BUCKET_READS, OBJECT_ACCESS
from .archive_permission_scenarios import denied
from .archive_storage_proof import atomic_json
from .archive_storage_scenarios import require, versions
from .rehearse_archive_permissions import PermissionResources, permission_template
from .rehearse_archive_storage import TAG, account_guard, preflight_outputs, session


def policy(bucket, account):
    arn = f"arn:aws:s3:::{bucket}"
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": list(BUCKET_READS), "Resource": arn,
         "Condition": {"StringEquals": {"s3:ResourceAccount": account}}},
        {"Effect": "Allow", "Action": list(OBJECT_ACCESS),
         "Resource": f"{arn}/{archive.PREFIX}*",
         "Condition": {"StringEquals": {"s3:ResourceAccount": account}}},
    ]}


def harness_policy(bucket, role_arn):
    # Only the separately approved disposable resources, never account-wide S3.
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": [
            "s3:CreateBucket", "s3:DeleteBucket", "s3:GetBucketLocation",
            "s3:GetBucketTagging", "s3:PutBucketTagging",
            "s3:GetBucketVersioning", "s3:PutBucketVersioning",
            "s3:GetBucketPublicAccessBlock", "s3:PutBucketPublicAccessBlock",
            "s3:GetBucketOwnershipControls", "s3:PutBucketOwnershipControls",
            "s3:GetEncryptionConfiguration", "s3:PutEncryptionConfiguration",
            "s3:GetLifecycleConfiguration", "s3:ListBucket", "s3:ListBucketVersions"],
         "Resource": f"arn:aws:s3:::{bucket}"},
        {"Effect": "Allow", "Action": [
            "s3:GetObject", "s3:GetObjectVersion", "s3:PutObject",
            "s3:DeleteObject", "s3:DeleteObjectVersion"],
         "Resource": f"arn:aws:s3:::{bucket}/{archive.PREFIX}*"},
        {"Effect": "Allow", "Action": "sts:AssumeRole", "Resource": role_arn},
    ]}


class ObservedClient:
    """Record real AWS conflicts; inject only a lost response AFTER a real write."""
    def __init__(self, client):
        self.client = client
        self.conflicts = 0
        self.lost = False
        self.injected = 0
        self.exact_reads = 0

    def __getattr__(self, name):
        return getattr(self.client, name)

    def put_object(self, **args):
        try:
            response = self.client.put_object(**args)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "PreconditionFailed":
                self.conflicts += 1
            raise
        if self.lost:
            self.lost = False
            self.injected += 1
            raise ConnectionClosedError(endpoint_url="https://s3.amazonaws.com")
        return response

    def get_object(self, **args):
        if "VersionId" in args:
            self.exact_reads += 1
        return self.client.get_object(**args)


def exercise(client, resources, directory):
    args = resources.args
    observed = ObservedClient(client)
    directory = Path(directory)
    fixture = {
        "course.html": b"<!doctype html><html><body>Synthetic course evidence</body></html>",
        "receipt.json": archive.encode({
            "started_at": datetime.now(timezone.utc).isoformat(),
            "cases": [{"name": "synthetic-storage-only", "passed": True}],
            "code_sha256": archive.sha(Path(archive.__file__).read_bytes()),
        }),
    }
    for name, data in fixture.items():
        (directory / name).write_bytes(data)
    inventory = {"schema_version": 1, "acceptance_id": "disposable-roundtrip",
                 "files": [{"path": name, "sha256": archive.sha(data)}
                           for name, data in fixture.items()]}

    def run():
        resources.owned()
        return archive.archive(observed, directory=directory, inventory=inventory,
                               bucket=args["Bucket"], owner=resources.account)

    resources.owned()
    denied(client.list_objects_v2, **args)
    denied(client.get_object, **args, Key=archive.PREFIX + "absent")
    first = run()
    before = versions(resources.s3, args)
    require(len(before) == 3, "expected two files and one manifest")
    require(run() == first and observed.conflicts == 3, "real conditional retries failed")
    require(versions(resources.s3, args) == before, "retry created extra versions")
    manifest_proof = first["manifest"]
    response = client.get_object(**args, Key=manifest_proof["key"],
                                 VersionId=manifest_proof["version_id"])
    try:
        raw = response["Body"].read()
    finally:
        response["Body"].close()
    require(archive.sha(raw) == manifest_proof["sha256"], "manifest hash mismatch")
    manifest = json.loads(raw)
    require(manifest["inventory_sha256"] == archive.sha(archive.encode(inventory)),
            "inventory hash mismatch")
    for entry in manifest["files"]:
        archive.verify(client, {**args, "Key": entry["key"],
                                "VersionId": entry["version_id"]}, fixture[entry["path"]])

    # A newer real object must not change what the pinned manifest downloads.
    entry = manifest["files"][0]
    resources.owned()
    corrupted = resources.s3.put_object(
        **args, Key=entry["key"], Body=b"changed", ServerSideEncryption="AES256",
        Metadata={"sha256": entry["sha256"]})
    archive.verify(client, {**args, "Key": entry["key"], "VersionId": entry["version_id"]},
                   fixture[entry["path"]])
    try:
        archive.verify(client, {**args, "Key": entry["key"],
                               "VersionId": corrupted["VersionId"]}, fixture[entry["path"]])
    except ValueError:
        pass
    else:
        raise AssertionError("corrupt version accepted")
    try:
        run()
    except ValueError:
        pass
    else:
        raise AssertionError("conflicting current object accepted")

    inventory["acceptance_id"] = "lost-response"
    observed.lost = True
    try:
        run()
    except ConnectionClosedError:
        pass
    else:
        raise AssertionError("lost response was not injected")
    require(observed.injected == 1, "no real write preceded lost response")
    recovered = run()
    require(recovered["status"] == "retained", "lost-response retry failed")
    require(len(versions(resources.s3, args)) == 7, "lost-response retry duplicated objects")
    for name, data in fixture.items():
        require((directory / name).read_bytes() == data, "local evidence changed")
    for operation, request in [
        (client.delete_object, {**args, "Key": entry["key"], "VersionId": entry["version_id"]}),
        (client.put_object, {**args, "Key": "outside-prefix/probe", "Body": b"test"}),
        (client.put_bucket_versioning, {**args, "VersioningConfiguration": {"Status": "Suspended"}}),
    ]:
        resources.owned()
        denied(operation, **request)
    archive.check_storage(client, args["Bucket"], resources.account)
    return {
        "checks": ["list_bucket_denied", "missing_key_access_denied", "conditional_412_retry",
                   "no_duplicate_retry_versions", "manifest_inventory_sha256",
                   "exact_version_download_after_overwrite", "corrupt_hash_refused",
                   "conflicting_current_object_refused", "lost_response_retry",
                   "local_evidence_unchanged", "remote_delete_denied",
                   "outside_prefix_write_denied", "bucket_mutation_denied"],
        "conditional_conflicts": observed.conflicts, "exact_version_reads": observed.exact_reads,
        "initial_archive": first, "recovered_archive": recovered,
        "manifest": manifest,
        "fault_injection": "client response loss after a real successful S3 PutObject",
    }


def run(*, account, production, region, run_id, output, approve=False, cleanup_only=False):
    require(approve, "explicit temporary permission approval required")
    output = Path(output)
    state_path, proof_path = output / f"{run_id}-state.json", output / f"{run_id}-proof.json"
    if not cleanup_only:
        preflight_outputs(state_path, proof_path)
    config = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 2},
                    ignore_configured_endpoint_urls=True)
    with ExitStack() as clients:
        sdk = session()
        def client(name, sdk=sdk):
            return clients.enter_context(closing(sdk.client(name, region_name=region, config=config)))
        resources = PermissionResources(sts=client("sts"), s3=client("s3"),
                                        cf=client("cloudformation"), iam=client("iam"),
                                        account=account, production=production,
                                        region=region, run_id=run_id)
        resources.account_guard()
        principal = resources.sts.get_caller_identity()["Arn"]
        require(principal.startswith(f"arn:aws:iam::{account}:user/"), "IAM user required")
        user = principal.rsplit("/", 1)[-1]
        grant = f"acceptance-rehearsal-{run_id}"
        role_arn = f"arn:aws:iam::{account}:role/{resources.state['role_name']}"
        resources.state.update(grant_name=grant, principal_arn=principal)
        if cleanup_only:
            saved = json.loads(state_path.read_text())
            require(all(saved.get(k) == v for k, v in resources.state.items()
                        if k != "cleanup_verified"), "saved ownership identity mismatch")
        else:
            require(grant not in resources.iam.list_user_policies(UserName=user)["PolicyNames"],
                    "temporary grant already exists")
            atomic_json(state_path, resources.state)
        result = None
        try:
            if not cleanup_only:
                resources.account_guard()
                resources.iam.put_user_policy(UserName=user, PolicyName=grant,
                    PolicyDocument=json.dumps(harness_policy(resources.state["bucket"], role_arn)))
                # Effective IAM and CloudFormation propagation are not immediate.
                time.sleep(60)
                resources.assert_absent()
                body = permission_template(run_id, resources.state["bucket"], account, principal)
                body["Resources"]["ArchiveRole"]["Properties"]["Policies"][0]["PolicyDocument"] = policy(
                    resources.state["bucket"], account)
                resources.account_guard()
                resources.cf.create_stack(
                    StackName=resources.state["stack_name"], TemplateBody=json.dumps(body),
                    Capabilities=["CAPABILITY_NAMED_IAM"], Tags=[{"Key": TAG, "Value": run_id}],
                    ClientRequestToken=f"acceptance-{run_id}", OnFailure="DO_NOTHING")
                resources.cf.get_waiter("stack_create_complete").wait(
                    StackName=resources.state["stack_name"],
                    WaiterConfig={"Delay": 5, "MaxAttempts": 120})
                resources.owned()
                resources.state["provisioned_at"] = datetime.now(timezone.utc).isoformat()
                atomic_json(state_path, resources.state)
                print("Disposable bucket and restricted role ready; waiting for versioning propagation.", flush=True)
                time.sleep(900)
                resources.owned_role()
                credentials = resources.sts.assume_role(
                    RoleArn=role_arn, RoleSessionName=f"acceptance-{run_id}", DurationSeconds=3600,
                    Policy=json.dumps(policy(resources.state["bucket"], account)))["Credentials"]
                restricted_sdk = boto3.Session(
                    aws_access_key_id=credentials["AccessKeyId"],
                    aws_secret_access_key=credentials["SecretAccessKey"],
                    aws_session_token=credentials["SessionToken"])
                restricted_sts = client("sts", restricted_sdk)
                account_guard(restricted_sts, account, production)
                require(restricted_sts.get_caller_identity()["Arn"].startswith(
                    f"arn:aws:sts::{account}:assumed-role/{resources.state['role_name']}/"),
                    "wrong restricted principal")
                with tempfile.TemporaryDirectory() as directory:
                    result = exercise(client("s3", restricted_sdk), resources, directory)
        finally:
            # Preserve the grant if resource cleanup fails so recovery remains possible.
            resources.cleanup()
            resources.account_guard()
            resources.iam.delete_user_policy(UserName=user, PolicyName=grant)
            require(grant not in resources.iam.list_user_policies(UserName=user)["PolicyNames"],
                    "temporary grant remains")
            resources.state["temporary_grant_removed"] = True
            atomic_json(state_path, resources.state)
        if result is not None:
            result.update(result="passed", kind="disposable_acceptance_evidence",
                          completed_at=datetime.now(timezone.utc).isoformat(),
                          code_sha256={name: archive.sha((Path(__file__).parent / name).read_bytes())
                                       for name in ("archive_acceptance_evidence.py",
                                                    "rehearse_acceptance_evidence.py")},
                          resources=resources.state)
            atomic_json(proof_path, result)
            print("Live acceptance round trip passed; resources and temporary grant removed.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("account", "production", "region", "output"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--run-id", default=uuid.uuid4().hex[:16])
    parser.add_argument("--approve-temporary-permissions", action="store_true")
    parser.add_argument("--cleanup-only", action="store_true")
    args = vars(parser.parse_args())
    args["approve"] = args.pop("approve_temporary_permissions")
    try:
        run(**args)
    except Exception as exc:
        code = exc.response["Error"]["Code"] if isinstance(exc, ClientError) else type(exc).__name__
        print(json.dumps({"result": "failed", "error_type": code,
                          "action": "inspect saved state and use cleanup-only if needed"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
