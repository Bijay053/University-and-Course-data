"""Least-privilege archive policy; not the disposable harness cleanup policy.

This builder performs no AWS calls. Attach only to a separately scoped role;
never update an existing production role as part of a rehearsal.
"""
import re

from .archive_smoke_receipts import PREFIX


BUCKET_READS = (
    "s3:GetBucketPublicAccessBlock",
    "s3:GetBucketVersioning",
    "s3:GetLifecycleConfiguration",
)
OBJECT_ACCESS = ("s3:PutObject", "s3:GetObject", "s3:GetObjectVersion")


def archive_policy(bucket, owner):
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise ValueError("explicit bucket required")
    if not re.fullmatch(r"\d{12}", owner):
        raise ValueError("explicit owner required")
    arn = f"arn:aws:s3:::{bucket}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "BucketSafetyReads",
                "Effect": "Allow",
                "Action": list(BUCKET_READS),
                "Resource": arn,
                "Condition": {"StringEquals": {"s3:ResourceAccount": owner}},
            },
            {
                "Sid": "ReceiptObjectsOnly",
                "Effect": "Allow",
                "Action": list(OBJECT_ACCESS),
                "Resource": f"{arn}/{PREFIX}*",
                "Condition": {"StringEquals": {"s3:ResourceAccount": owner}},
            },
        ],
    }
