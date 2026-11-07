"""Private document uploads. Tokens bind immutable object versions to an actor/job."""
import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid

from app.config import settings
from app.services import snapshot_store

MAX_BYTES = 2_000_000


def sign(payload):
    raw = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    signature = hmac.new(settings.session_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return raw + "." + signature


def verify(token, actor_id, job_id):
    try:
        raw, signature = token.split(".")
        expected = hmac.new(settings.session_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        data = json.loads(base64.urlsafe_b64decode(raw))
        if data["actor"] != actor_id or data["job"] != job_id or data["expires"] < time.time():
            raise ValueError()
        if not data["key"].startswith(f"assistant-documents/{actor_id}/{job_id}/"):
            raise ValueError()
        return data
    except Exception as exc:
        raise ValueError("Document attachment expired or belongs to another user/review. Upload it again.") from exc


async def request_upload(actor_id, job_id, filename, size, content_type, *, direct=False):
    if not filename.lower().endswith(".pdf") or content_type != "application/pdf":
        raise ValueError("Upload a PDF document.")
    if not 0 < size <= MAX_BYTES:
        raise ValueError("PDF must be between 1 byte and 2 MB.")
    if not snapshot_store.is_enabled():
        raise ValueError("Private document storage is unavailable.")
    key = f"assistant-documents/{actor_id}/{job_id}/{uuid.uuid4().hex}.pdf"
    data = dict(key=key, actor=actor_id, job=job_id, filename=filename[:180],
                size=size, expires=time.time() + 3600)
    if not direct:
        # Same-origin authenticated transport requires no browser access to S3.
        # The bucket, object permissions and existing private read path stay unchanged.
        return {"transport": "portal", "documentToken": sign(data)}
    def generate():
        client = snapshot_store._make_client()
        try:
            return client.generate_presigned_post(
                Bucket=snapshot_store._bucket(), Key=key,
                Fields={"Content-Type": "application/pdf"},
                Conditions=[{"Content-Type": "application/pdf"}, ["content-length-range", size, size]],
                ExpiresIn=900,
            )
        finally:
            client.close()
    upload = await asyncio.to_thread(generate)
    return {"uploadUrl": upload["url"], "fields": upload["fields"], "documentToken": sign(data)}


async def store_uploaded(token, actor_id, job_id, raw):
    data = verify(token, actor_id, job_id)
    if not isinstance(raw, bytes) or len(raw) != data["size"] or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError("Uploaded file size does not match the prepared attachment.")
    if not raw.lstrip().startswith(b"%PDF-"):
        raise ValueError("Upload a valid PDF document.")
    if not snapshot_store.is_enabled():
        raise ValueError("Private document storage is unavailable.")
    def upload():
        client = snapshot_store._make_client()
        try:
            # A grant is write-once: retries cannot replace bytes that were
            # already read, matched or included in an approval preview.
            client.put_object(
                Bucket=snapshot_store._bucket(), Key=data["key"], Body=raw,
                ContentType="application/pdf", IfNoneMatch="*",
            )
        finally:
            client.close()
    await asyncio.to_thread(upload)
    return {"uploaded": True}


async def read_uploaded(token, actor_id, job_id):
    data = verify(token, actor_id, job_id)
    def download():
        client = snapshot_store._make_client()
        try:
            response = client.get_object(Bucket=snapshot_store._bucket(), Key=data["key"])
            body = response["Body"]
            try:
                if response["ContentLength"] != data["size"] or response["ContentLength"] > MAX_BYTES:
                    raise ValueError("Uploaded file size changed.")
                raw = body.read(MAX_BYTES + 1)
            finally:
                body.close()
            if len(raw) != data["size"] or not raw.lstrip().startswith(b"%PDF"):
                raise ValueError("Uploaded file is not the declared PDF.")
            return raw
        finally:
            client.close()
    raw = await asyncio.to_thread(download)
    from app.services.scrape_assistant_tools import inspect_pdf
    source = await inspect_pdf(raw, "uploaded-document:" + hashlib.sha256(raw).hexdigest())
    source.update(provenance="staff-uploaded-document", filename=data["filename"],
                  officialProvenanceVerified=False, _objectKey=data["key"])
    return source
