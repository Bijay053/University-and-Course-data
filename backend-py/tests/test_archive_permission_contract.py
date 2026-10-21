"""Offline IAM shape + actual archive through botocore's validated SDK Stubber.

These are contracts, not evidence of AWS authorization enforcement. Live proof
must use a distinct assumed role and an independently fenced cleanup identity.
"""
import hashlib
from contextlib import closing
from fnmatch import fnmatchcase
import io
import json
import os

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from deploy import archive_smoke_receipts as archive
from deploy.archive_permission_contract import archive_policy

BUCKET = "disposable-archive-contract"
OWNER = "123456789012"
ARGS = {"Bucket": BUCKET, "ExpectedBucketOwner": OWNER}


def test_policy_has_exact_permissions_and_resources():
    policy = archive_policy(BUCKET, OWNER)
    bucket, objects = policy["Statement"]
    assert policy["Version"] == "2012-10-17"
    assert set(bucket["Action"]) == {
        "s3:GetBucketPublicAccessBlock", "s3:GetBucketVersioning",
        "s3:GetLifecycleConfiguration",
    }
    assert set(objects["Action"]) == {
        "s3:PutObject", "s3:GetObject", "s3:GetObjectVersion",
    }
    assert bucket["Resource"] == f"arn:aws:s3:::{BUCKET}"
    assert objects["Resource"] == f"arn:aws:s3:::{BUCKET}/{archive.PREFIX}*"
    for statement in policy["Statement"]:
        assert statement["Effect"] == "Allow"
        assert statement["Condition"] == {"StringEquals": {"s3:ResourceAccount": OWNER}}


@pytest.mark.parametrize("bucket,owner", [("*", OWNER), (BUCKET, "*"), ("bad/name", OWNER)])
def test_invalid_policy_scope_is_rejected(bucket, owner):
    with pytest.raises(ValueError):
        archive_policy(bucket, owner)


@pytest.mark.parametrize("action,resource", [
    ("s3:DeleteObject", f"arn:aws:s3:::{BUCKET}/{archive.PREFIX}receipt.json"),
    ("s3:DeleteObjectVersion", f"arn:aws:s3:::{BUCKET}/{archive.PREFIX}receipt.json"),
    ("s3:PutBucketPolicy", f"arn:aws:s3:::{BUCKET}"),
    ("s3:DeleteBucketPolicy", f"arn:aws:s3:::{BUCKET}"),
    ("s3:PutLifecycleConfiguration", f"arn:aws:s3:::{BUCKET}"),
    ("s3:ListBucket", f"arn:aws:s3:::{BUCKET}"),
    ("s3:PutObject", f"arn:aws:s3:::{BUCKET}/outside/receipt.json"),
    ("s3:GetObject", f"arn:aws:s3:::{BUCKET}/outside/receipt.json"),
    ("s3:GetObjectVersion", f"arn:aws:s3:::{BUCKET}/outside/receipt.json"),
    ("s3:PutObject", f"arn:aws:s3:::another-bucket/{archive.PREFIX}receipt.json"),
])
def test_contract_does_not_grant_mutation_deletion_or_out_of_scope_access(action, resource):
    # Deliberately only tests this policy's grants; not an IAM simulator and not
    # a substitute for live denials under the actual assumed-role credentials.
    assert not any(
        statement["Effect"] == "Allow" and action in statement["Action"]
        and fnmatchcase(resource, statement["Resource"])
        for statement in archive_policy(BUCKET, OWNER)["Statement"]
    )


def fixture(directory, number):
    job = f"safe_restart_smoke_{number:032x}"
    record = {"kind": "ordinary_smoke_acceptance", "schema_version": 1,
              "database": {"runtime_job_id": job}, "celery": {"job_id": job}}
    path = directory / f"{job}.json"
    path.write_text(json.dumps(record))
    os.utime(path, (number, number))
    data = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    digest = hashlib.sha256(data).hexdigest()
    args = {**ARGS, "Key": f"{archive.PREFIX}{job}/{digest}.json"}
    return path, data, digest, args


def safety(stub):
    stub.add_response("get_public_access_block", {"PublicAccessBlockConfiguration": {
        k: True for k in ("BlockPublicAcls", "IgnorePublicAcls",
                         "BlockPublicPolicy", "RestrictPublicBuckets")}}, ARGS)
    stub.add_response("get_bucket_versioning", {"Status": "Enabled"}, ARGS)
    stub.add_client_error("get_bucket_lifecycle_configuration",
                          service_error_code="NoSuchLifecycleConfiguration",
                          http_status_code=404, expected_params=ARGS)


def read(stub, data, digest, args):
    stub.add_response("get_object", {
        "Body": StreamingBody(io.BytesIO(data), len(data)),
        "Metadata": {"sha256": digest}, "VersionId": "version-1",
    }, args)


def denied(stub, operation, args):
    stub.add_client_error(operation, service_error_code="AccessDenied",
                          http_status_code=403, expected_params=args)


def put_args(data, digest, args):
    return {**args, "Body": data, "ContentType": "application/json",
            "ServerSideEncryption": "AES256", "Metadata": {"sha256": digest},
            "IfNoneMatch": "*"}


def test_no_list_permission_upload_retry_and_retention(tmp_path):
    old, new = fixture(tmp_path, 1), fixture(tmp_path, 2)
    # Explicit inert credentials prevent any SDK credential discovery or network.
    with closing(boto3.client("s3", region_name="us-east-1", aws_access_key_id="offline",
                             aws_secret_access_key="offline")) as client, Stubber(client) as stub:
        safety(stub)
        for path, data, digest, args in (new, old):
            denied(stub, "get_object", args)  # Missing key, no ListBucket
            stub.add_response("put_object", {"VersionId": "version-1"},
                              put_args(data, digest, args))
            denied(stub, "get_object", {**args, "VersionId": "version-1"})
        result = archive.archive_directory(client, directory=tmp_path, bucket=BUCKET,
                                            owner=OWNER, keep=1)
        assert result["failed"] == 2
        assert old[0].exists() and new[0].exists()
        safety(stub)
        for path, data, digest, args in (new, old):
            read(stub, data, digest, args)
            if path == old[0]:
                read(stub, data, digest, {**args, "VersionId": "version-1"})
        assert archive.archive_directory(client, directory=tmp_path, bucket=BUCKET,
                                          owner=OWNER, keep=1) == {
            "archived": 2, "failed": 0, "removed_local": 1, "retained_local": 1}
        assert not old[0].exists() and new[0].exists()
        stub.assert_no_pending_responses()


def test_denied_read_cannot_overwrite_existing_evidence_or_prune(tmp_path):
    receipts = [fixture(tmp_path, i) for i in (1, 2)]
    with closing(boto3.client("s3", region_name="us-east-1", aws_access_key_id="offline",
                             aws_secret_access_key="offline")) as client, Stubber(client) as stub:
        safety(stub)
        for path, data, digest, args in reversed(receipts):
            denied(stub, "get_object", args)
            stub.add_client_error("put_object", service_error_code="PreconditionFailed",
                                  http_status_code=412,
                                  expected_params=put_args(data, digest, args))
            denied(stub, "get_object", args)
        result = archive.archive_directory(client, directory=tmp_path, bucket=BUCKET,
                                            owner=OWNER, keep=1)
        assert result["failed"] == 2 and result["removed_local"] == 0
        assert all(item[0].exists() for item in receipts)
        stub.assert_no_pending_responses()
