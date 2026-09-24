#!/usr/bin/env python3
"""Transfer a signed disposable rehearsal receipt to the protected host.

SSM sees only a public certificate, encrypted CMS bytes, and fixed status tokens.
The production host validates against its own pinned signer and template before
atomically replacing the root-owned release-gate receipt.
"""
from __future__ import annotations

import argparse
import base64
import os
import re
import shlex
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from database_refresh_rehearsal_proof import (
    PRODUCTION_ACCOUNT_IDS,
    validate_rehearsal_proof,
)
from rehearse_database_secret_refresh import (
    TAG_KEY,
    _exclude_terminated_instance_residues,
)

DEPLOY_DIR = Path(__file__).resolve().parent
HOST_DEPLOY_DIR = "/opt/university-portal/backend-py/deploy"
HOST_PROOF = "/etc/university-portal/database-refresh-rehearsal-proof.json"
HOST_TRANSFER_DIR = "/root/.university-portal-rehearsal-transfer"
_INSTANCE_ID = re.compile(r"i-[0-9a-f]{8,17}\Z")


def _production_session():
    key = os.environ.get("AWS_SSM_ACCESS_KEY_ID")
    secret = os.environ.get("AWS_SSM_SECRET_ACCESS_KEY")
    token = os.environ.get("AWS_SSM_SESSION_TOKEN")
    if not key or not secret:
        raise RuntimeError("dedicated AWS_SSM production credentials are required")
    return boto3.Session(
        aws_access_key_id=key, aws_secret_access_key=secret,
        aws_session_token=token or None,
    )


def _identity(session, region: str) -> str:
    return session.client("sts", region_name=region).get_caller_identity()["Account"]


def _verify_teardown(session, region: str, account: str, run_id: str) -> None:
    if _identity(session, region) != account or account in PRODUCTION_ACCOUNT_IDS:
        raise RuntimeError("disposable account identity mismatch")
    cf = session.client("cloudformation", region_name=region)
    try:
        stacks = cf.describe_stacks(
            StackName=f"up-db-refresh-rehearsal-{run_id}"
        )["Stacks"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ValidationError" or "does not exist" not in exc.response["Error"].get("Message", ""):
            raise
    else:
        if len(stacks) != 1 or stacks[0]["StackStatus"] != "DELETE_COMPLETE":
            raise RuntimeError("disposable rehearsal stack has not been deleted")
    tagging = session.client("resourcegroupstaggingapi", region_name=region)
    ec2 = session.client("ec2", region_name=region)
    residues = []
    token = ""
    while True:
        request = {"TagFilters": [{"Key": TAG_KEY, "Values": [run_id]}]}
        if token:
            request["PaginationToken"] = token
        page = tagging.get_resources(**request)
        residues.extend(page["ResourceTagMappingList"])
        token = page.get("PaginationToken", "")
        if not token:
            break
    if _exclude_terminated_instance_residues(ec2, residues):
        raise RuntimeError("disposable rehearsal resources remain")


def _command(ssm, instance_id: str, commands: list[str], expected: str) -> str:
    response = ssm.send_command(
        InstanceIds=[instance_id], DocumentName="AWS-RunShellScript",
        Comment="Transfer signed database refresh rehearsal proof",
        Parameters={"commands": commands},
    )
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            result = ssm.get_command_invocation(
                CommandId=response["Command"]["CommandId"], InstanceId=instance_id
            )
        except ssm.exceptions.InvocationDoesNotExist:
            time.sleep(1)
            continue
        if result["Status"] in {"Pending", "InProgress", "Delayed"}:
            time.sleep(1)
            continue
        if result["Status"] != "Success":
            raise RuntimeError("rehearsal receipt handoff SSM command failed")
        output = result.get("StandardOutputContent", "").strip()
        if expected == "certificate":
            if len(output) > 16000 or not output:
                raise RuntimeError("rehearsal receipt handoff certificate is invalid")
        elif output != expected:
            raise RuntimeError("rehearsal receipt handoff returned unexpected status")
        return output
    raise TimeoutError("rehearsal receipt handoff timed out; inspect host receipt before retry")


def handoff(
    proof_path: Path, *, disposable_account: str, production_account: str,
    instance_id: str, region: str, disposable_session=None, production_session=None,
) -> None:
    if (
        not re.fullmatch(r"[0-9]{12}", disposable_account)
        or not re.fullmatch(r"[0-9]{12}", production_account)
        or production_account not in PRODUCTION_ACCOUNT_IDS
        or disposable_account == production_account
        or not _INSTANCE_ID.fullmatch(instance_id)
    ):
        raise RuntimeError("explicit disposable and protected production identities are required")
    proof = validate_rehearsal_proof(
        proof_path, expected_account_id=disposable_account,
        template_path=DEPLOY_DIR / "database-secret-refresh-rehearsal.yaml",
    )
    if proof_path.stat().st_size > 65536:
        raise RuntimeError("rehearsal receipt exceeds size limit")
    if proof["region"] != region:
        raise RuntimeError("rehearsal region does not match handoff region")
    # No production mutation until the disposable identity and deletion are checked.
    if disposable_session is None:
        from rehearse_database_secret_refresh import _session
        disposable_session = _session()
    _verify_teardown(disposable_session, region, disposable_account, proof["run_id"])
    production_session = production_session or _production_session()
    if _identity(production_session, region) != production_account:
        raise RuntimeError("production account identity mismatch")
    ec2 = production_session.client("ec2", region_name=region)
    reservations = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"]
    instances = [i for r in reservations for i in r["Instances"]]
    if len(instances) != 1 or instances[0]["State"]["Name"] != "running":
        raise RuntimeError("protected production instance is not running")
    ssm = production_session.client("ssm", region_name=region)
    transfer_id = uuid.uuid4().hex
    remote_dir = HOST_TRANSFER_DIR
    key = f"{remote_dir}/{transfer_id}.key"
    cert = f"{remote_dir}/{transfer_id}.pem"
    certificate = _command(ssm, instance_id, [
        "set -eu",
        "umask 077",
        f"install -d -m 700 {shlex.quote(remote_dir)}",
        f"openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 1 -subj /CN=rehearsal-transfer -keyout {shlex.quote(key)} -out {shlex.quote(cert)} >/dev/null 2>&1",
        f"base64 -w0 {shlex.quote(cert)}",
    ], "certificate")
    try:
        _install_encrypted_receipt(
            ssm, instance_id, proof_path, certificate, key, cert,
            disposable_account,
        )
    except BaseException:
        # Preparation can succeed even when local encryption or transfer fails.
        # Cleanup is best effort; never hide the original failure or print output.
        try:
            _command(
                ssm, instance_id,
                ["set -eu", f"rm -f {shlex.quote(key)} {shlex.quote(cert)}",
                 "printf cleaned"], "cleaned",
            )
        except Exception:
            pass
        raise


def _install_encrypted_receipt(
    ssm, instance_id: str, proof_path: Path, certificate: str,
    key: str, cert: str, disposable_account: str,
) -> None:
    # Public certificate is the only non-status SSM output. Never log it.
    with tempfile.TemporaryDirectory() as directory:
        recipient = Path(directory) / "recipient.pem"
        recipient.write_bytes(base64.b64decode(certificate, validate=True))
        encrypted = subprocess.run(
            ["openssl", "cms", "-encrypt", "-binary", "-aes-256-cbc",
             "-outform", "DER", str(recipient)],
            input=proof_path.read_bytes(), capture_output=True, check=True,
        ).stdout
    encoded = base64.b64encode(encrypted).decode("ascii")
    # Python validation emits no receipt data; a failed or interrupted install
    # leaves the old receipt untouched. The temp file is removed on every exit.
    script = f"""import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, {HOST_DEPLOY_DIR!r})
from database_refresh_rehearsal_proof import validate_rehearsal_proof
destination = Path({HOST_PROOF!r})
destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
fd, temporary = tempfile.mkstemp(prefix=".database-refresh-rehearsal-", dir=destination.parent)
try:
    with os.fdopen(fd, "wb") as stream:
        stream.write(sys.stdin.buffer.read(65537))
        stream.flush()
        os.fsync(stream.fileno())
    if os.stat(temporary).st_size > 65536:
        raise RuntimeError("receipt exceeds size limit")
    validate_rehearsal_proof(Path(temporary), expected_account_id={disposable_account!r},
        template_path=Path({HOST_DEPLOY_DIR!r}) / "database-secret-refresh-rehearsal.yaml")
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)
    directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
"""
    # The script itself is non-secret; the ciphertext is never emitted as output.
    encoded_script = base64.b64encode(script.encode()).decode("ascii")
    install = [
        "set -eu",
        "set +x",
        "umask 077",
        f"trap 'rm -f {shlex.quote(key)} {shlex.quote(cert)}' EXIT",
        # Decode the fixed validator as code; decrypted receipt enters only stdin.
        f"printf %s {shlex.quote(encoded)} | base64 -d | "
        f"openssl cms -decrypt -binary -inform DER -recip {shlex.quote(cert)} -inkey {shlex.quote(key)} 2>/dev/null | "
        f"python3 -c \"$(printf %s {shlex.quote(encoded_script)} | base64 -d)\" "
        ">/dev/null 2>&1"
    ]
    install += [
        "printf installed",
    ]
    _command(ssm, instance_id, install, "installed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proof", type=Path, required=True)
    parser.add_argument("--disposable-account-id", required=True)
    parser.add_argument("--production-account-id", required=True)
    parser.add_argument("--production-instance-id", required=True)
    parser.add_argument("--region", default="ap-south-1")
    args = parser.parse_args()
    handoff(args.proof, disposable_account=args.disposable_account_id,
            production_account=args.production_account_id,
            instance_id=args.production_instance_id, region=args.region)
    print("signed rehearsal proof installed on protected production host")


if __name__ == "__main__":
    main()