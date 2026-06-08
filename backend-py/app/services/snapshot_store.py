"""S3-compatible snapshot storage for university page HTML/JSON/PDF snapshots.

Designed to work with AWS S3 today and Cloudflare R2 tomorrow — both expose
the same S3 API, so switching only requires changing the endpoint_url env var.

Storage layout
--------------
  universities/{university_id}/{scrape_job_id}/{url_hash}/rendered.html
  universities/{university_id}/{scrape_job_id}/{url_hash}/raw.html
  universities/{university_id}/{scrape_job_id}/api/{endpoint_hash}/page_1.json
  universities/{university_id}/{scrape_job_id}/pdf/{file_hash}/document.pdf

Lifecycle tags (applied per object so S3 lifecycle rules can act on them)
--------------------------------------------------------------------------
  snapshot_type=html        → 90 days
  snapshot_type=json        → 12 months
  snapshot_type=pdf         → 12 months
  snapshot_type=repair      → 180 days
  snapshot_type=failed      → 180 days

Configuration (env / secrets)
-----------------------------
  AWS_ACCESS_KEY_ID
  AWS_SECRET_ACCESS_KEY
  AWS_S3_BUCKET_NAME
  AWS_S3_REGION
  AWS_S3_ENDPOINT_URL   (optional — set to R2 endpoint to switch providers)
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Literal

log = logging.getLogger(__name__)

SnapshotType = Literal["html", "json", "pdf", "repair", "failed"]

_BUCKET: str | None = None
_ENABLED: bool | None = None


def _bucket() -> str:
    global _BUCKET
    if _BUCKET is None:
        _BUCKET = os.environ.get("AWS_S3_BUCKET_NAME", "")
    return _BUCKET


def is_enabled() -> bool:
    """Return True only when all required env vars are present."""
    global _ENABLED
    if _ENABLED is None:
        _ENABLED = bool(
            os.environ.get("AWS_ACCESS_KEY_ID")
            and os.environ.get("AWS_SECRET_ACCESS_KEY")
            and os.environ.get("AWS_S3_BUCKET_NAME")
            and os.environ.get("AWS_S3_REGION")
        )
    return _ENABLED


def url_hash(url: str) -> str:
    """Stable 16-char hex digest of a URL — used as the S3 path segment."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def build_s3_key(
    university_id: int,
    scrape_job_id: str,
    url: str,
    snapshot_type: SnapshotType,
    *,
    page_number: int = 1,
) -> str:
    """Construct the canonical S3 object key for a snapshot."""
    h = url_hash(url)
    if snapshot_type == "json":
        return f"universities/{university_id}/{scrape_job_id}/api/{h}/page_{page_number}.json"
    if snapshot_type == "pdf":
        return f"universities/{university_id}/{scrape_job_id}/pdf/{h}/document.pdf"
    # html / repair / failed
    suffix = "rendered.html" if snapshot_type == "html" else f"{snapshot_type}.html"
    return f"universities/{university_id}/{scrape_job_id}/{h}/{suffix}"


def _make_client():
    """Create a synchronous boto3 S3 client (used for background thread tasks)."""
    import boto3  # type: ignore[import]

    kwargs: dict = {
        "region_name": os.environ.get("AWS_S3_REGION", "us-east-1"),
        "aws_access_key_id": os.environ.get("AWS_ACCESS_KEY_ID"),
        "aws_secret_access_key": os.environ.get("AWS_SECRET_ACCESS_KEY"),
    }
    endpoint = os.environ.get("AWS_S3_ENDPOINT_URL")
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("s3", **kwargs)


def _make_async_session():
    """Create an aioboto3 session for async S3 operations."""
    import aioboto3  # type: ignore[import]

    return aioboto3.Session(
        region_name=os.environ.get("AWS_S3_REGION", "us-east-1"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    )


def _tagging_str(snapshot_type: SnapshotType) -> str:
    return f"snapshot_type={snapshot_type}"


async def upload_snapshot(
    content: str | bytes,
    *,
    university_id: int,
    scrape_job_id: str,
    url: str,
    snapshot_type: SnapshotType,
    content_type: str = "text/html; charset=utf-8",
    page_number: int = 1,
) -> str | None:
    """Upload a snapshot to S3.  Returns the S3 key on success, None on failure.

    Never raises — snapshot upload failure must never break a live scrape.
    """
    if not is_enabled():
        return None
    key = build_s3_key(university_id, scrape_job_id, url, snapshot_type, page_number=page_number)
    bucket = _bucket()
    body = content.encode("utf-8") if isinstance(content, str) else content
    try:
        session = _make_async_session()
        endpoint = os.environ.get("AWS_S3_ENDPOINT_URL")
        extra: dict = {}
        if endpoint:
            extra["endpoint_url"] = endpoint
        async with session.client("s3", **extra) as s3:
            await s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                Tagging=_tagging_str(snapshot_type),
            )
        log.info("snapshot uploaded: s3://%s/%s (%d bytes)", bucket, key, len(body))
        return key
    except Exception as exc:
        log.warning("snapshot upload failed for %s: %s", url, exc)
        return None


async def download_snapshot(key: str) -> bytes | None:
    """Download a snapshot from S3 by key.  Returns raw bytes or None on failure."""
    if not is_enabled():
        return None
    bucket = _bucket()
    try:
        session = _make_async_session()
        endpoint = os.environ.get("AWS_S3_ENDPOINT_URL")
        extra: dict = {}
        if endpoint:
            extra["endpoint_url"] = endpoint
        async with session.client("s3", **extra) as s3:
            resp = await s3.get_object(Bucket=bucket, Key=key)
            data = await resp["Body"].read()
        return data
    except Exception as exc:
        log.warning("snapshot download failed for key %s: %s", key, exc)
        return None


async def presign_url(key: str, expires_in: int = 3600) -> str | None:
    """Generate a pre-signed URL for direct browser download (used in the portal diff view)."""
    if not is_enabled():
        return None
    bucket = _bucket()
    try:
        session = _make_async_session()
        endpoint = os.environ.get("AWS_S3_ENDPOINT_URL")
        extra: dict = {}
        if endpoint:
            extra["endpoint_url"] = endpoint
        async with session.client("s3", **extra) as s3:
            url = await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        return url
    except Exception as exc:
        log.warning("presign failed for key %s: %s", key, exc)
        return None


async def list_snapshots_for_job(
    university_id: int, scrape_job_id: str
) -> list[dict]:
    """List all snapshot objects under universities/{uni_id}/{job_id}/."""
    if not is_enabled():
        return []
    bucket = _bucket()
    prefix = f"universities/{university_id}/{scrape_job_id}/"
    try:
        session = _make_async_session()
        endpoint = os.environ.get("AWS_S3_ENDPOINT_URL")
        extra: dict = {}
        if endpoint:
            extra["endpoint_url"] = endpoint
        async with session.client("s3", **extra) as s3:
            paginator = s3.get_paginator("list_objects_v2")
            objects: list[dict] = []
            async for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    objects.append({
                        "key": obj["Key"],
                        "size": obj["Size"],
                        "last_modified": obj["LastModified"].isoformat(),
                    })
        return objects
    except Exception as exc:
        log.warning("list_snapshots failed for job %s: %s", scrape_job_id, exc)
        return []


def setup_lifecycle_rules() -> bool:
    """Apply S3 lifecycle rules for automatic snapshot expiry.

    Call this once during initial setup or via a management script.
    Returns True on success, False on failure.

    Lifecycle rules:
      html snapshots    → expire after 90 days
      json snapshots    → expire after 365 days
      pdf snapshots     → expire after 365 days
      repair snapshots  → expire after 180 days
      failed snapshots  → expire after 180 days
    """
    if not is_enabled():
        log.warning("S3 not configured — skipping lifecycle rule setup")
        return False
    bucket = _bucket()
    try:
        client = _make_client()
        rules = [
            {
                "ID": "expire-html-snapshots-90d",
                "Status": "Enabled",
                "Filter": {"Tag": {"Key": "snapshot_type", "Value": "html"}},
                "Expiration": {"Days": 90},
            },
            {
                "ID": "expire-json-snapshots-365d",
                "Status": "Enabled",
                "Filter": {"Tag": {"Key": "snapshot_type", "Value": "json"}},
                "Expiration": {"Days": 365},
            },
            {
                "ID": "expire-pdf-snapshots-365d",
                "Status": "Enabled",
                "Filter": {"Tag": {"Key": "snapshot_type", "Value": "pdf"}},
                "Expiration": {"Days": 365},
            },
            {
                "ID": "expire-repair-snapshots-180d",
                "Status": "Enabled",
                "Filter": {"Tag": {"Key": "snapshot_type", "Value": "repair"}},
                "Expiration": {"Days": 180},
            },
            {
                "ID": "expire-failed-snapshots-180d",
                "Status": "Enabled",
                "Filter": {"Tag": {"Key": "snapshot_type", "Value": "failed"}},
                "Expiration": {"Days": 180},
            },
        ]
        client.put_bucket_lifecycle_configuration(
            Bucket=bucket,
            LifecycleConfiguration={"Rules": rules},
        )
        log.info("S3 lifecycle rules applied to bucket %s", bucket)
        return True
    except Exception as exc:
        log.warning("Failed to apply lifecycle rules to %s: %s", bucket, exc)
        return False
