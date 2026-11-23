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
MAX_ATTACHMENTS = 5
UPLOAD_TYPES = {
    "application/pdf": (".pdf",),
    "image/png": (".png",),
    "image/jpeg": (".jpg", ".jpeg"),
}


def validate_bytes(raw, content_type):
    if content_type == "application/pdf":
        if not raw.lstrip().startswith(b"%PDF-"):
            raise ValueError("Upload a valid PDF document.")
    elif content_type in {"image/png", "image/jpeg"}:
        from app.services.assistant_image_review import validate_image
        validate_image(raw, content_type)
    else:
        raise ValueError("Attach a PDF, PNG or JPEG file.")


def sign(payload):
    raw = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    signature = hmac.new(settings.session_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return raw + "." + signature


def verify(token, actor_id, job_id, *, allow_expired=False):
    try:
        raw, signature = token.split(".")
        expected = hmac.new(settings.session_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        data = json.loads(base64.urlsafe_b64decode(raw))
        if data["actor"] != actor_id or data["job"] != job_id or (not allow_expired and data["expires"] < time.time()):
            raise ValueError()
        if not data["key"].startswith(f"assistant-documents/{actor_id}/{job_id}/"):
            raise ValueError()
        return data
    except Exception as exc:
        raise ValueError("Document attachment expired or belongs to another user/review. Upload it again.") from exc


async def request_upload(actor_id, job_id, filename, size, content_type, *, direct=False):
    if content_type not in UPLOAD_TYPES or not filename.lower().endswith(UPLOAD_TYPES[content_type]):
        raise ValueError("Attach a PDF, PNG or JPEG file.")
    if not 0 < size <= MAX_BYTES:
        raise ValueError("PDF must be between 1 byte and 2 MB.")
    if not snapshot_store.is_enabled():
        raise ValueError("Private document storage is unavailable.")
    key = f"assistant-documents/{actor_id}/{job_id}/{uuid.uuid4().hex}{UPLOAD_TYPES[content_type][0]}"
    data = dict(key=key, actor=actor_id, job=job_id, filename=filename[:180],
                size=size, contentType=content_type, expires=time.time() + 3600)
    if not direct:
        # Same-origin authenticated transport requires no browser access to S3.
        # The bucket, object permissions and existing private read path stay unchanged.
        return {"transport": "portal", "documentToken": sign(data)}
    def generate():
        client = snapshot_store._make_client()
        try:
            return client.generate_presigned_post(
                Bucket=snapshot_store._bucket(), Key=key,
                Fields={"Content-Type": content_type},
                Conditions=[{"Content-Type": content_type}, ["content-length-range", size, size]],
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
    content_type = data.get("contentType", "application/pdf")
    validate_bytes(raw, content_type)
    if not snapshot_store.is_enabled():
        raise ValueError("Private document storage is unavailable.")
    def upload():
        client = snapshot_store._make_client()
        try:
            # A grant is write-once: retries cannot replace bytes that were
            # already read, matched or included in an approval preview.
            client.put_object(
                Bucket=snapshot_store._bucket(), Key=data["key"], Body=raw,
                ContentType=content_type, IfNoneMatch="*",
            )
        finally:
            client.close()
    await asyncio.to_thread(upload)
    return {"uploaded": True}


async def read_bytes(data):
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
            if len(raw) != data["size"]:
                raise ValueError("Uploaded file size changed.")
            validate_bytes(raw, data.get("contentType", "application/pdf"))
            return raw
        finally:
            client.close()
    return await asyncio.to_thread(download)


def message_image(message, actor_id, job_id):
    """Only resolve a signed attachment saved on this owner's user message."""
    return next((data for _, data in message_attachments(message, actor_id, job_id)
                 if data.get("contentType") in {"image/png", "image/jpeg"}), None)


def document_tokens(message):
    """The old single-file contract remains readable, including saved history."""
    tokens = message.get("_documentTokens")
    if isinstance(tokens, list):
        return tokens[:MAX_ATTACHMENTS]
    return [message["_documentToken"]] if message.get("_documentToken") else []


def validate_tokens(tokens, actor_id, job_id):
    if not 1 <= len(tokens) <= MAX_ATTACHMENTS:
        raise ValueError("Attach between one and five files.")
    if any(not isinstance(token, str) or not 0 < len(token) <= 4000 for token in tokens):
        raise ValueError("Invalid document attachment.")
    documents = [verify(token, actor_id, job_id) for token in tokens]
    if len({d["key"] for d in documents}) != len(documents):
        raise ValueError("The same attachment was included more than once.")
    if any(d.get("contentType") not in UPLOAD_TYPES or not 0 < d.get("size", 0) <= MAX_BYTES
           for d in documents) or sum(d["size"] for d in documents) > MAX_ATTACHMENTS * MAX_BYTES:
        raise ValueError("Attach up to five files, no larger than 2 MB each.")
    return documents


def message_attachments(message, actor_id, job_id):
    if message.get("role") != "user" or not message.get("turnId"):
        return []
    found = []
    for index, token in enumerate(document_tokens(message)):
        try:
            data = verify(token, actor_id, job_id, allow_expired=True)
        except ValueError:
            continue
        if data.get("contentType") in UPLOAD_TYPES:
            found.append((index, data))
    return found


def public_attachments(message, actor_id, job_id):
    return [{"index": index, **{key: data[key] for key in ("filename", "contentType", "size")}}
            for index, data in message_attachments(message, actor_id, job_id)]


async def read_uploaded(token, actor_id, job_id):
    data = verify(token, actor_id, job_id)
    raw = await read_bytes(data)
    content_type = data.get("contentType", "application/pdf")
    if content_type.startswith("image/"):
        return {
            "sourceType": "image", "verified": False,
            "officialProvenanceVerified": False, "provenance": "staff-uploaded-image",
            "filename": data["filename"], "_objectKey": data["key"],
            "_imageDataUrl": f"data:{content_type};base64," + base64.b64encode(raw).decode(),
        }
    from app.services.scrape_assistant_tools import inspect_pdf
    source = await inspect_pdf(raw, "uploaded-document:" + hashlib.sha256(raw).hexdigest())
    source.update(provenance="staff-uploaded-document", filename=data["filename"],
                  officialProvenanceVerified=False, _objectKey=data["key"])
    return source
