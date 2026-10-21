"""Domain-separated, pinned KMS proof for the storage-only archive rehearsal."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .database_refresh_rehearsal_proof import (
    PRODUCTION_ACCOUNT_IDS, SIGNING_ALGORITHM, _canonical_payload, _load_signer,
)

KIND = "disposable_archive_storage"
CHECKS = (
    "versioned_conditional_upload", "sha256_readback", "lost_response_retry",
    "public_access_refusal", "lifecycle_refusal", "local_retention",
)


def atomic_json(path, payload):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def code_digest():
    directory = Path(__file__).parent
    digest = hashlib.sha256()
    for name in ("archive_smoke_receipts.py", "archive_storage_scenarios.py",
                 "archive_storage_proof.py", "rehearse_archive_storage.py",
                 "archive_permission_contract.py", "archive_permission_scenarios.py",
                 "rehearse_archive_permissions.py"):
        digest.update(name.encode())
        digest.update((directory / name).read_bytes())
    return digest.hexdigest()


def pinned_key(kms, key_id, account, region, signers):
    metadata = kms.describe_key(KeyId=key_id)["KeyMetadata"]
    arn = metadata.get("Arn", "")
    parts = arn.split(":")
    if (
        len(parts) != 6 or parts[2:5] != ["kms", region, account]
        or not parts[5].startswith("key/")
        or metadata.get("KeyUsage") != "SIGN_VERIFY"
        or metadata.get("KeySpec") not in {"RSA_2048", "RSA_3072", "RSA_4096"}
        or not metadata.get("Enabled") or account in PRODUCTION_ACCOUNT_IDS
    ):
        raise ValueError("invalid disposable signing key")
    pem = _load_signer(Path(signers), account_id=account, signing_key_arn=arn)
    return arn, serialization.load_pem_public_key(pem.encode())


def write_proof(path, *, kms, key_id, account, region, signers, state, checks, permissions=False):
    from .archive_permission_scenarios import CHECKS as PERMISSION_CHECKS
    expected = PERMISSION_CHECKS if permissions else CHECKS
    if tuple(checks) != expected or state.get("cleanup_verified") is not True:
        raise ValueError("archive checks and cleanup must pass before signing")
    arn, public = pinned_key(kms, key_id, account, region, signers)
    payload = {
        "kind": "disposable_archive_permissions" if permissions else KIND,
        "schema_version": 1, "result": "passed",
        "account_id": account, "region": region, "run_id": state["run_id"],
        "bucket": state["bucket"], "stack_name": state["stack_name"],
        "teardown_verified": True, "checks": list(checks),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "code_sha256": code_digest(), "signing_key_arn": arn,
        "signing_algorithm": SIGNING_ALGORITHM,
    }
    signature = kms.sign(
        KeyId=arn, Message=_canonical_payload(payload), MessageType="RAW",
        SigningAlgorithm=SIGNING_ALGORITHM,
    )["Signature"]
    public.verify(signature, _canonical_payload(payload),
                  padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                              salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    payload["signature_base64"] = base64.b64encode(signature).decode("ascii")
    atomic_json(path, payload)
    return payload


def validate_proof(path, *, account, run_id, signers, permissions=False):
    """Offline verification against independently pinned keys, never receipt-supplied keys."""
    payload = json.loads(Path(path).read_text())
    from .archive_permission_scenarios import CHECKS as PERMISSION_CHECKS
    required = {
        "kind": "disposable_archive_permissions" if permissions else KIND,
        "schema_version": 1, "result": "passed",
        "account_id": account, "run_id": run_id, "teardown_verified": True,
        "checks": list(PERMISSION_CHECKS if permissions else CHECKS), "code_sha256": code_digest(),
        "signing_algorithm": SIGNING_ALGORITHM,
    }
    if account in PRODUCTION_ACCOUNT_IDS or any(payload.get(k) != v for k, v in required.items()):
        raise ValueError("archive proof identity or evidence mismatch")
    completed = datetime.fromisoformat(payload["completed_at"])
    now = datetime.now(timezone.utc)
    if completed.tzinfo is None or not -timedelta(minutes=5) <= now - completed <= timedelta(hours=24):
        raise ValueError("archive proof is stale or future-dated")
    public = serialization.load_pem_public_key(_load_signer(
        Path(signers), account_id=account, signing_key_arn=payload["signing_key_arn"],
    ).encode())
    signature = base64.b64decode(payload.pop("signature_base64"), validate=True)
    public.verify(signature, _canonical_payload(payload),
                  padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                              salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return payload
