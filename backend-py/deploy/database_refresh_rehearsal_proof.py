"""Create and verify KMS-signed database-refresh rehearsal receipts."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

PROOF_SCHEMA_VERSION = 1
SIGNER_SCHEMA_VERSION = 1
DEFAULT_MAX_AGE = timedelta(hours=24)
SIGNING_ALGORITHM = "RSASSA_PSS_SHA_256"
PRODUCTION_ACCOUNT_IDS = frozenset({"905043442097"})
DEFAULT_SIGNERS_PATH = Path(__file__).with_name(
    "database-refresh-rehearsal-signers.json"
)
_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_RUN_ID = re.compile(r"^[0-9a-f]{16}$")
_REGION = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z]+-[0-9]+$")


class RehearsalProofError(RuntimeError):
    """The rehearsal proof is missing, stale, unsigned, or does not match."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _template_digest(template_path: Path) -> str:
    return hashlib.sha256(template_path.read_bytes()).hexdigest()


def _canonical_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _load_signer(
    signers_path: Path, *, account_id: str, signing_key_arn: str
) -> str:
    try:
        document = json.loads(signers_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RehearsalProofError(
            "pinned disposable rehearsal signer configuration is unavailable"
        ) from exc
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != SIGNER_SCHEMA_VERSION
        or not isinstance(document.get("signers"), list)
    ):
        raise RehearsalProofError(
            "pinned disposable rehearsal signer configuration is invalid"
        )
    matches = [
        signer
        for signer in document["signers"]
        if isinstance(signer, dict)
        and signer.get("account_id") == account_id
        and signer.get("signing_key_arn") == signing_key_arn
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("public_key_pem"), str):
        raise RehearsalProofError(
            "rehearsal signer is not pinned for this disposable account"
        )
    return matches[0]["public_key_pem"]


def write_rehearsal_proof(
    proof_path: Path,
    *,
    session: Any,
    signing_key_id: str,
    account_id: str,
    region: str,
    run_id: str,
    template_path: Path,
    completed_at: datetime | None = None,
) -> dict[str, Any]:
    """Atomically emit a KMS-signed non-secret proof after successful teardown."""
    if not _ACCOUNT_ID.fullmatch(account_id) or account_id in PRODUCTION_ACCOUNT_IDS:
        raise RehearsalProofError("rehearsal account ID is invalid or production")
    if not _RUN_ID.fullmatch(run_id):
        raise RehearsalProofError("rehearsal run ID is invalid")
    if not _REGION.fullmatch(region):
        raise RehearsalProofError("rehearsal region is invalid")
    completed = completed_at or _utc_now()
    if completed.tzinfo is None:
        raise RehearsalProofError("rehearsal completion time must be timezone-aware")
    kms = session.client("kms", region_name=region)
    metadata = kms.describe_key(KeyId=signing_key_id)["KeyMetadata"]
    signing_key_arn = str(metadata.get("Arn", ""))
    arn_parts = signing_key_arn.split(":")
    if (
        len(arn_parts) < 6
        or arn_parts[2] != "kms"
        or arn_parts[3] != region
        or arn_parts[4] != account_id
        or metadata.get("KeyUsage") != "SIGN_VERIFY"
        or metadata.get("KeySpec") not in {"RSA_2048", "RSA_3072", "RSA_4096"}
        or not metadata.get("Enabled")
    ):
        raise RehearsalProofError(
            "KMS proof-signing key is not an enabled RSA signing key in the disposable account"
        )
    payload: dict[str, Any] = {
        "schema_version": PROOF_SCHEMA_VERSION,
        "result": "passed",
        "teardown_verified": True,
        "account_id": account_id,
        "region": region,
        "run_id": run_id,
        "completed_at": completed.astimezone(timezone.utc).isoformat(),
        "template_sha256": _template_digest(template_path),
        "signing_algorithm": SIGNING_ALGORITHM,
        "signing_key_arn": signing_key_arn,
    }
    signature = kms.sign(
        KeyId=signing_key_arn,
        Message=_canonical_payload(payload),
        MessageType="RAW",
        SigningAlgorithm=SIGNING_ALGORITHM,
    )["Signature"]
    payload["signature_base64"] = base64.b64encode(signature).decode("ascii")
    proof_path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{proof_path.name}.", dir=proof_path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, proof_path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return payload


def validate_rehearsal_proof(
    proof_path: Path,
    *,
    expected_account_id: str,
    template_path: Path,
    signers_path: Path = DEFAULT_SIGNERS_PATH,
    max_age: timedelta = DEFAULT_MAX_AGE,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fail closed unless a recent proof has a pinned disposable KMS signature."""
    if not _ACCOUNT_ID.fullmatch(expected_account_id):
        raise RehearsalProofError("expected rehearsal account ID is required")
    if expected_account_id in PRODUCTION_ACCOUNT_IDS:
        raise RehearsalProofError("production account cannot attest a rehearsal")
    try:
        payload = json.loads(proof_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RehearsalProofError("database refresh rehearsal proof is unavailable") from exc
    if not isinstance(payload, dict):
        raise RehearsalProofError("database refresh rehearsal proof has invalid shape")
    required = {
        "schema_version": PROOF_SCHEMA_VERSION,
        "result": "passed",
        "teardown_verified": True,
        "account_id": expected_account_id,
        "template_sha256": _template_digest(template_path),
        "signing_algorithm": SIGNING_ALGORITHM,
    }
    if any(payload.get(key) != value for key, value in required.items()):
        raise RehearsalProofError(
            "database refresh rehearsal proof does not match this account or template"
        )
    if not _RUN_ID.fullmatch(str(payload.get("run_id", ""))):
        raise RehearsalProofError("database refresh rehearsal proof has invalid run ID")
    if not _REGION.fullmatch(str(payload.get("region", ""))):
        raise RehearsalProofError("database refresh rehearsal proof has invalid region")
    try:
        completed = datetime.fromisoformat(str(payload["completed_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RehearsalProofError(
            "database refresh rehearsal proof has invalid completion time"
        ) from exc
    if completed.tzinfo is None:
        raise RehearsalProofError(
            "database refresh rehearsal proof completion time lacks timezone"
        )
    current = (now or _utc_now()).astimezone(timezone.utc)
    completed = completed.astimezone(timezone.utc)
    if completed > current + timedelta(minutes=5):
        raise RehearsalProofError("database refresh rehearsal proof is future-dated")
    if current - completed > max_age:
        raise RehearsalProofError("database refresh rehearsal proof is stale")
    signing_key_arn = str(payload.get("signing_key_arn", ""))
    public_key_pem = _load_signer(
        signers_path,
        account_id=expected_account_id,
        signing_key_arn=signing_key_arn,
    )
    encoded_signature = payload.pop("signature_base64", None)
    try:
        signature = base64.b64decode(encoded_signature, validate=True)
        public_key = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
        public_key.verify(
            signature,
            _canonical_payload(payload),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise RehearsalProofError(
            "database refresh rehearsal proof signature is invalid"
        ) from exc
    payload["signature_base64"] = encoded_signature
    return payload