"""Archive exercised exclusively through a narrow role; observer owns cleanup."""
from pathlib import Path

from botocore.exceptions import ClientError

from . import archive_smoke_receipts as archive
from .archive_storage_scenarios import FaultClient, receipt, require, versions

CHECKS = (
    "restricted_role_upload", "restricted_role_conditional_retry",
    "restricted_role_lost_response_retry", "restricted_role_local_retention",
    "restricted_role_failed_read_retention", "remote_delete_denied",
    "bucket_policy_mutation_denied", "bucket_lifecycle_mutation_denied",
    "outside_prefix_access_denied",
)


def denied(operation, **args):
    try:
        operation(**args)
    except ClientError as exc:
        require(exc.response["Error"]["Code"] == "AccessDenied"
                and exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 403,
                "expected actual AWS AccessDenied/403")
    else:
        raise RuntimeError("restricted role unexpectedly permitted forbidden operation")


def exercise(client, *, observer, bucket, owner, directory, owned):
    args = {"Bucket": bucket, "ExpectedBucketOwner": owner}

    def folder(name):
        path = Path(directory) / name
        path.mkdir()
        return path

    def run(path, storage=client, keep=1):
        owned()
        return archive.archive_directory(storage, directory=path, bucket=bucket,
                                         owner=owner, keep=keep)

    path = folder("conditional")
    first = receipt(path, 1)
    fault = FaultClient(client, "race")
    require(run(path, fault)["archived"] == 1 and fault.conflicts == 1,
            "conditional upload under restricted role failed")
    proof = archive.archive_one(client, bucket, owner, first)
    require(len(versions(observer, {**args, "Prefix": proof["args"]["Key"]})) == 1,
            "conditional retry created extra versions")

    path = folder("lost-response")
    old, new = receipt(path, 1), receipt(path, 2)
    before = {p.name: p.read_bytes() for p in (old, new)}
    fault = FaultClient(client, "lost")
    require(run(path, fault)["failed"] == 2 and fault.injected == 2,
            "lost-response fault was not exercised")
    require({p.name: p.read_bytes() for p in (old, new)} == before,
            "failed uploads removed local evidence")
    before_versions = versions(observer, args)
    require(run(path, fault) == {"archived": 2, "failed": 0,
                                "removed_local": 1, "retained_local": 1},
            "restricted retry/retention failed")
    require(fault.puts == 2 and versions(observer, args) == before_versions
            and not old.exists() and new.exists(), "retry duplicated or lost evidence")

    path = folder("failed-read")
    old, new = receipt(path, 1), receipt(path, 2)
    require(run(path, keep=2)["archived"] == 2, "read-failure setup failed")
    fault = FaultClient(client, "unreadable", old)
    result = run(path, fault)
    require(result["failed"] == 1 and result["removed_local"] == 0
            and fault.injected == 1 and old.exists() and new.exists(),
            "failed version read removed local evidence")

    # These requests must reach AWS with the restricted role, not a deny adapter.
    # Every mutation attempt is fenced even though all are expected to fail.
    calls = [
        (client.delete_object, proof["args"]),
        (client.delete_object, {**proof["args"], "VersionId": proof["version"]}),
        (client.put_bucket_policy, {**args, "Policy":
            '{"Version":"2012-10-17","Statement":[{"Effect":"Deny","Principal":"*",'
            '"Action":"s3:DeleteObject","Resource":"arn:aws:s3:::' + bucket + '/*"}]}'}),
        (client.delete_bucket_policy, args),
        (client.put_bucket_lifecycle_configuration, {**args, "LifecycleConfiguration": {
            "Rules": [{"ID": "denial-probe", "Status": "Disabled",
                       "Filter": {"Prefix": archive.PREFIX}, "Expiration": {"Days": 365}}]}}),
        (client.delete_bucket_lifecycle, args),
        (client.put_object, {**args, "Key": "outside-prefix/probe", "Body": b"synthetic"}),
        (client.get_object, {**args, "Key": "outside-prefix/probe"}),
    ]
    before_versions = versions(observer, args)
    for operation, request in calls:
        owned()
        denied(operation, **request)
    require(versions(observer, args) == before_versions,
            "forbidden probes changed evidence")
    archive._verify(client, {**proof["args"], "VersionId": proof["version"]}, proof["data"])
    return CHECKS
