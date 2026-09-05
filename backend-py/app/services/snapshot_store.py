"""S3-compatible snapshot storage for university page HTML/JSON/PDF snapshots.

Designed to work with AWS S3 today and Cloudflare R2 tomorrow — both expose
the same S3 API, so switching only requires changing the endpoint_url env var.

Storage layout (type-prefix scheme)
------------------------------------
  HTML courses:   universities/{uni_id}/{job_id}/html/{url_hash}.html.gz
  Repair passes:  universities/{uni_id}/{job_id}/repair/{url_hash}.html.gz
  Failed fetches: universities/{uni_id}/{job_id}/failed/{url_hash}.html.gz
  API/JSON:       universities/{uni_id}/{job_id}/api/{endpoint_hash}/page_N.json.gz
  PDF documents:  universities/{uni_id}/{job_id}/pdf/{file_hash}/document.pdf

The type is always the 4th path segment (after universities/uni_id/job_id/).
This makes objects human-readable in the S3 console and enables targeted
manual cleanup by type prefix.

Lifecycle rules (tag-based — applied via setup_lifecycle_rules())
-----------------------------------------------------------------
  Each upload is tagged with snapshot_type=<type-segment> so S3 lifecycle
  rules can expire objects by type regardless of the dynamic uni_id/job_id
  segments that precede the type in the path:

  Tag                   Expiry   Path segment
  snapshot_type=html    90 days  /html/
  snapshot_type=repair  180 days /repair/
  snapshot_type=api     365 days /api/
  snapshot_type=pdf     365 days /pdf/
  snapshot_type=failed  30 days  /failed/

Compression
-----------
  HTML, JSON, and repair snapshots are gzip-compressed before upload
  (ContentEncoding: gzip, .gz suffix in key).  PDFs are already compressed.

  Storage estimate at 300-uni scale:
    300 unis × 200 courses × 150 KB (gzip avg) × 52 scrapes/year ≈ 468 GB/year raw
    With 90-day lifecycle expiry: ~115 GB retained at any time
    S3 Standard @ $0.023/GB/month ≈ $2.65/month ≈ $32/year

Configuration (env / secrets)
-----------------------------
  AWS_ACCESS_KEY_ID
  AWS_SECRET_ACCESS_KEY
  AWS_S3_BUCKET_NAME
  AWS_S3_REGION
  AWS_S3_ENDPOINT_URL   (optional — set to R2 endpoint to switch providers)
  SNAPSHOT_ENABLED      (optional — set to false/0/no/off to disable globally)
"""
from __future__ import annotations

import gzip
import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Literal

log = logging.getLogger(__name__)

SnapshotType = Literal["html", "json", "pdf", "repair", "failed", "ai_prompt"]
SnapshotAvailability = Literal["available", "expired", "missing", "unavailable"]

_RETENTION_DAYS: dict[str, int] = {
    "html": 90,
    "repair": 180,
    "json": 365,
    "api": 365,
    "pdf": 365,
    "failed": 30,
    "ai_prompt": 90,
}
_SNAPSHOT_LIFECYCLE_RULE_IDS = frozenset({
    "expire-html-snapshots-90d",
    "expire-repair-snapshots-180d",
    "expire-api-snapshots-365d",
    "expire-pdf-snapshots-365d",
    "expire-failed-snapshots-30d",
    "expire-ai-prompt-snapshots-90d",
})

_BUCKET: str | None = None
_ENABLED: bool | None = None


def _bucket() -> str:
    global _BUCKET
    if _BUCKET is None:
        _BUCKET = os.environ.get("AWS_S3_BUCKET_NAME", "")
    return _BUCKET


def is_enabled() -> bool:
    """Return True when snapshot storage is active.

    Enabled when all four AWS credentials are present AND the
    kill-switch env var SNAPSHOT_ENABLED is not explicitly set to
    'false' or '0'.  Not cached — env vars can be injected after import.
    """
    if os.environ.get("SNAPSHOT_ENABLED", "").lower() in ("false", "0", "no", "off"):
        return False
    return bool(
        os.environ.get("AWS_ACCESS_KEY_ID")
        and os.environ.get("AWS_SECRET_ACCESS_KEY")
        and os.environ.get("AWS_S3_BUCKET_NAME")
        and os.environ.get("AWS_S3_REGION")
    )


def url_hash(url: str) -> str:
    """Stable 16-char hex digest of a URL — used as the S3 path segment."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _lifecycle_tag(snapshot_type: SnapshotType) -> str:
    """Map snapshot_type code to the path-segment name used for tagging.

    json objects live under /api/ so their tag is 'api' to match the
    lifecycle rule that targets the /api/ path segment.
    """
    if snapshot_type == "json":
        return "api"
    if snapshot_type == "ai_prompt":
        return "ai_prompt"
    return snapshot_type


def build_s3_key(
    university_id: int,
    scrape_job_id: str,
    url: str,
    snapshot_type: SnapshotType,
    *,
    page_number: int = 1,
) -> str:
    """Construct the canonical S3 object key for a snapshot.

    Type is the 4th path segment so lifecycle rules and manual inspection
    can target objects by type without relying on file extension alone:

      html/repair/failed → universities/{uid}/{job}/html/{hash}.html.gz
      json (API)         → universities/{uid}/{job}/api/{hash}/page_N.json.gz
      pdf                → universities/{uid}/{job}/pdf/{hash}/document.pdf
    """
    h = url_hash(url)
    if snapshot_type == "json":
        return f"universities/{university_id}/{scrape_job_id}/api/{h}/page_{page_number}.json.gz"
    if snapshot_type == "pdf":
        return f"universities/{university_id}/{scrape_job_id}/pdf/{h}/document.pdf"
    if snapshot_type == "ai_prompt":
        return f"universities/{university_id}/{scrape_job_id}/ai_prompt/{h}/chunk_{page_number}.txt.gz"
    # html / repair / failed — type is the prefix segment
    return f"universities/{university_id}/{scrape_job_id}/{snapshot_type}/{h}.html.gz"


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


def expected_expiry_at(
    snapshot_type: str,
    fetched_at: datetime | None,
) -> datetime | None:
    """Return the lifecycle deadline for a stored snapshot, when known."""
    retention_days = _RETENTION_DAYS.get(snapshot_type)
    if retention_days is None or fetched_at is None:
        return None
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    return fetched_at + timedelta(days=retention_days)


async def snapshot_availability(
    storage_path: str | None,
    *,
    snapshot_type: str,
    fetched_at: datetime | None,
    now: datetime | None = None,
) -> dict[str, str | bool | None]:
    """Check whether a DB snapshot reference still has a stored S3 object.

    A missing object is considered expired only after its configured lifecycle
    deadline. Storage configuration and transient provider failures are
    reported as unavailable rather than falsely labelling the object missing.
    """
    expires_at = expected_expiry_at(snapshot_type, fetched_at)
    result: dict[str, str | bool | None] = {
        "availability": "missing",
        "available": False,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }
    if not storage_path:
        return result
    if not is_enabled():
        result["availability"] = "unavailable"
        return result

    bucket = _bucket()
    try:
        session = _make_async_session()
        endpoint = os.environ.get("AWS_S3_ENDPOINT_URL")
        extra: dict = {}
        if endpoint:
            extra["endpoint_url"] = endpoint
        async with session.client("s3", **extra) as s3:
            await s3.head_object(Bucket=bucket, Key=storage_path)
        result["availability"] = "available"
        result["available"] = True
        return result
    except Exception as exc:
        error = getattr(exc, "response", {}).get("Error", {})
        code = str(error.get("Code", ""))
        status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code not in {"404", "NoSuchKey", "NotFound"} and status != 404:
            log.warning("snapshot availability check failed for %s: %s", storage_path, exc)
            result["availability"] = "unavailable"
            return result

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if expires_at is not None and current >= expires_at:
        result["availability"] = "expired"
    return result


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
    raw = content.encode("utf-8") if isinstance(content, str) else content

    # Gzip-compress HTML and JSON to reduce storage by ~70%.
    # PDF is already compressed; skip it.
    compress = snapshot_type in ("html", "json", "repair", "failed", "ai_prompt")
    if compress:
        body = gzip.compress(raw, compresslevel=6)
        encoding_header: dict = {"ContentEncoding": "gzip"}
    else:
        body = raw
        encoding_header = {}

    tag_value = _lifecycle_tag(snapshot_type)

    try:
        session = _make_async_session()
        endpoint = os.environ.get("AWS_S3_ENDPOINT_URL")
        extra: dict = {}
        if endpoint:
            extra["endpoint_url"] = endpoint
        async with session.client("s3", **extra) as s3:
            # Step 1 — upload (requires s3:PutObject only).
            await s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                **encoding_header,
            )
            # Step 2 — tag (requires s3:PutObjectTagging; best-effort).
            # Tags enable lifecycle rules to expire objects by type even when
            # the type segment is buried after dynamic university_id/job_id
            # path segments.  A tagging failure is logged but never blocks
            # the scrape — the object is still safely stored in S3.
            try:
                await s3.put_object_tagging(
                    Bucket=bucket,
                    Key=key,
                    Tagging={"TagSet": [{"Key": "snapshot_type", "Value": tag_value}]},
                )
            except Exception as tag_exc:
                log.warning(
                    "snapshot tagging skipped for %s (grant s3:PutObjectTagging "
                    "to enable lifecycle rules): %s",
                    key, tag_exc,
                )
        log.info(
            "snapshot uploaded: s3://%s/%s tag=%s raw=%d gz=%d bytes",
            bucket, key, tag_value, len(raw), len(body),
        )
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
        # Decompress gzip-encoded objects transparently
        if resp.get("ContentEncoding") == "gzip":
            data = gzip.decompress(data)
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

    Rules use tag-based filtering (snapshot_type=<type>) because the type
    segment sits after dynamic university_id/job_id segments in the key, so
    S3 prefix rules alone cannot target all objects of a given type across
    all universities and jobs.  Every upload now sets the Tagging parameter,
    so all new objects match these tag filters.

    Lifecycle rules (aligns with path segment naming):
      snapshot_type=html    → expire after 90 days   (/html/)
      snapshot_type=repair  → expire after 180 days  (/repair/)
      snapshot_type=api     → expire after 365 days  (/api/)
      snapshot_type=pdf     → expire after 365 days  (/pdf/)
      snapshot_type=failed  → expire after 30 days   (/failed/)

    Required IAM permission: s3:PutLifecycleConfiguration
    If the IAM user lacks this permission, apply rules manually:
      AWS Console → S3 → <bucket> → Management → Lifecycle rules
    """
    if not is_enabled():
        log.warning("S3 not configured — skipping lifecycle rule setup")
        return False
    bucket = _bucket()
    try:
        client = _make_client()
        snapshot_rules = [
            {
                "ID": "expire-html-snapshots-90d",
                "Status": "Enabled",
                "Filter": {"Tag": {"Key": "snapshot_type", "Value": "html"}},
                "Expiration": {"Days": 90},
            },
            {
                "ID": "expire-repair-snapshots-180d",
                "Status": "Enabled",
                "Filter": {"Tag": {"Key": "snapshot_type", "Value": "repair"}},
                "Expiration": {"Days": 180},
            },
            {
                "ID": "expire-api-snapshots-365d",
                "Status": "Enabled",
                "Filter": {"Tag": {"Key": "snapshot_type", "Value": "api"}},
                "Expiration": {"Days": 365},
            },
            {
                "ID": "expire-pdf-snapshots-365d",
                "Status": "Enabled",
                "Filter": {"Tag": {"Key": "snapshot_type", "Value": "pdf"}},
                "Expiration": {"Days": 365},
            },
            {
                "ID": "expire-failed-snapshots-30d",
                "Status": "Enabled",
                "Filter": {"Tag": {"Key": "snapshot_type", "Value": "failed"}},
                "Expiration": {"Days": 30},
            },
            {
                "ID": "expire-ai-prompt-snapshots-90d",
                "Status": "Enabled",
                "Filter": {"Tag": {"Key": "snapshot_type", "Value": "ai_prompt"}},
                "Expiration": {"Days": 90},
            },
        ]
        try:
            current = client.get_bucket_lifecycle_configuration(Bucket=bucket)
            existing_rules = current.get("Rules", [])
        except Exception as exc:
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            if code != "NoSuchLifecycleConfiguration":
                raise
            current = {}
            existing_rules = []
        unrelated_rules = [
            rule
            for rule in existing_rules
            if rule.get("ID") not in _SNAPSHOT_LIFECYCLE_RULE_IDS
        ]
        rules = [*unrelated_rules, *snapshot_rules]
        if len(rules) > 1000:
            raise RuntimeError("S3 lifecycle rule limit would be exceeded")
        lifecycle_configuration = {"Rules": rules}
        if "TransitionDefaultMinimumObjectSize" in current:
            lifecycle_configuration["TransitionDefaultMinimumObjectSize"] = current[
                "TransitionDefaultMinimumObjectSize"
            ]
        client.put_bucket_lifecycle_configuration(
            Bucket=bucket,
            LifecycleConfiguration=lifecycle_configuration,
        )
        log.info(
            "S3 lifecycle rules applied to bucket %s (%d unrelated rules preserved)",
            bucket,
            len(unrelated_rules),
        )
        return True
    except Exception as exc:
        log.warning("Failed to apply lifecycle rules to %s: %s", bucket, exc)
        return False


async def get_snapshot_bytes(storage_path: str) -> bytes | None:
    """Download the raw (gzip-compressed) bytes for a snapshot object from S3.

    Caller is responsible for decompression.
    Never raises — returns None on any failure.
    """
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
            resp = await s3.get_object(Bucket=bucket, Key=storage_path)
            return await resp["Body"].read()
    except Exception as exc:
        log.warning("get_snapshot_bytes failed for %s: %s", storage_path, exc)
        return None
