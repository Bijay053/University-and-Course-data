"""Signed receipt handoff and host-side atomic installation contracts."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone
from unittest.mock import Mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
sys.path.insert(0, str(DEPLOY))
import database_refresh_rehearsal_proof as proof  # noqa: E402
import handoff_database_refresh_rehearsal as handoff  # noqa: E402
import rehearse_database_secret_refresh as rehearsal  # noqa: E402

DISPOSABLE = "151955775532"
PRODUCTION = "905043442097"
RUN = "1234567890abcdef"
REGION = "ap-south-1"


def _signed_receipt(tmp_path: Path) -> tuple[Path, Path]:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    template = deploy / "database-secret-refresh-rehearsal.yaml"
    template.write_bytes((DEPLOY / template.name).read_bytes())
    (deploy / "database_refresh_rehearsal_proof.py").write_bytes(
        (DEPLOY / "database_refresh_rehearsal_proof.py").read_bytes()
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    arn = f"arn:aws:kms:{REGION}:{DISPOSABLE}:key/test"
    (deploy / "database-refresh-rehearsal-signers.json").write_text(json.dumps({
        "schema_version": 1, "signers": [{
            "account_id": DISPOSABLE, "signing_key_arn": arn,
            "public_key_pem": key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode(),
        }],
    }))
    payload = {
        "schema_version": 1, "result": "passed", "teardown_verified": True,
        "account_id": DISPOSABLE, "region": REGION, "run_id": RUN,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "template_sha256": proof._template_digest(template),
        "signing_algorithm": proof.SIGNING_ALGORITHM, "signing_key_arn": arn,
    }
    signature = key.sign(
        proof._canonical_payload(payload),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    payload["signature_base64"] = base64.b64encode(signature).decode()
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(payload))
    return deploy, receipt


def _local_signer(monkeypatch, deploy):
    original = proof.validate_rehearsal_proof
    monkeypatch.setattr(
        handoff, "validate_rehearsal_proof",
        lambda path, **kwargs: original(
            path, signers_path=deploy / "database-refresh-rehearsal-signers.json",
            **kwargs,
        ),
    )


class _Session:
    def __init__(self, account: str, *, residues=None, stack_status="DELETE_COMPLETE"):
        self.account = account
        self.residues = residues or []
        self.stack_status = stack_status

    def client(self, name, region_name):
        assert region_name == REGION
        if name == "sts":
            return Mock(get_caller_identity=lambda: {"Account": self.account})
        if name == "cloudformation":
            return Mock(describe_stacks=lambda **kw: {
                "Stacks": [{"StackStatus": self.stack_status}]
            })
        if name == "resourcegroupstaggingapi":
            return Mock(get_resources=lambda **kw: {
                "ResourceTagMappingList": self.residues
            })
        if name == "ec2":
            return Mock(describe_instances=lambda **kw: {
                "Reservations": [{"Instances": [{"State": {"Name": "running"}}]}
                ] if "InstanceIds" in kw else []
            })
        if name == "ssm":
            return Mock()
        raise AssertionError(name)


@pytest.mark.parametrize("account,stack,residues", [
    ("000000000000", "DELETE_COMPLETE", []),
    (DISPOSABLE, "DELETE_IN_PROGRESS", []),
    (DISPOSABLE, "DELETE_COMPLETE", [{"ResourceARN": "arn:aws:s3:::leftover"}]),
])
def test_teardown_fails_closed(account, stack, residues):
    with pytest.raises(RuntimeError):
        handoff._verify_teardown(
            _Session(account, residues=residues, stack_status=stack),
            REGION, DISPOSABLE, RUN,
        )


def test_wrong_production_identity_never_sends_command(tmp_path, monkeypatch):
    deploy, receipt = _signed_receipt(tmp_path)
    monkeypatch.setattr(handoff, "DEPLOY_DIR", deploy)
    _local_signer(monkeypatch, deploy)
    monkeypatch.setattr(handoff, "_verify_teardown", Mock())
    with pytest.raises(RuntimeError, match="production account identity"):
        handoff.handoff(
            receipt, disposable_account=DISPOSABLE, production_account=PRODUCTION,
            instance_id="i-12345678", region=REGION,
            disposable_session=_Session(DISPOSABLE),
            production_session=_Session("000000000000"),
        )


def test_invalid_signature_never_checks_aws_or_sends_command(tmp_path, monkeypatch):
    deploy, receipt = _signed_receipt(tmp_path)
    monkeypatch.setattr(handoff, "DEPLOY_DIR", deploy)
    _local_signer(monkeypatch, deploy)
    contents = json.loads(receipt.read_text())
    contents["result"] = "failed"
    receipt.write_text(json.dumps(contents))
    check = Mock()
    monkeypatch.setattr(handoff, "_verify_teardown", check)
    with pytest.raises(proof.RehearsalProofError):
        handoff.handoff(
            receipt, disposable_account=DISPOSABLE, production_account=PRODUCTION,
            instance_id="i-12345678", region=REGION,
        )
    check.assert_not_called()


@pytest.mark.parametrize("failure", [None, "interrupted", "host-rejects"])
def test_host_validates_before_atomic_install(tmp_path, monkeypatch, failure):
    deploy, receipt = _signed_receipt(tmp_path)
    host_deploy = deploy
    if failure == "host-rejects":
        host_deploy = tmp_path / "host-deploy"
        host_deploy.mkdir()
        for leaf in deploy.iterdir():
            (host_deploy / leaf.name).write_bytes(leaf.read_bytes())
        (host_deploy / "database-refresh-rehearsal-signers.json").write_text(
            '{"schema_version":1,"signers":[]}'
        )
    destination = tmp_path / "protected" / "receipt.json"
    destination.parent.mkdir()
    destination.write_text("old receipt")
    monkeypatch.setattr(handoff, "DEPLOY_DIR", deploy)
    monkeypatch.setattr(handoff, "HOST_DEPLOY_DIR", str(host_deploy))
    monkeypatch.setattr(handoff, "HOST_PROOF", str(destination))
    monkeypatch.setattr(handoff, "HOST_TRANSFER_DIR", str(tmp_path / "transfer"))
    _local_signer(monkeypatch, deploy)
    monkeypatch.setattr(handoff, "_verify_teardown", Mock())
    observed = []

    def run_command(_ssm, _instance_id, commands, expected):
        observed.append(commands)
        if failure == "interrupted" and expected == "installed":
            raise TimeoutError("interrupted")
        result = subprocess.run(
            "\n".join(commands), shell=True, executable="/bin/bash",
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise RuntimeError("SSM failed (output intentionally suppressed)")
        return result.stdout.strip()

    monkeypatch.setattr(handoff, "_command", run_command)
    # Avoid writing test ephemeral keys to the real root transfer directory.
    monkeypatch.setattr(handoff, "uuid", Mock(uuid4=lambda: Mock(hex="testtransfer")))
    remote = tmp_path / "transfer"
    try:
        if failure:
            with pytest.raises((TimeoutError, RuntimeError)):
                handoff.handoff(
                    receipt, disposable_account=DISPOSABLE, production_account=PRODUCTION,
                    instance_id="i-12345678", region=REGION,
                    disposable_session=_Session(DISPOSABLE),
                    production_session=_Session(PRODUCTION),
                )
        else:
            handoff.handoff(
            receipt, disposable_account=DISPOSABLE, production_account=PRODUCTION,
            instance_id="i-12345678", region=REGION,
            disposable_session=_Session(DISPOSABLE),
            production_session=_Session(PRODUCTION),
        )
    finally:
        for leaf in remote.glob("testtransfer.*"):
            leaf.unlink()
    if failure:
        assert destination.read_text() == "old receipt"
    else:
        proof.validate_rehearsal_proof(
            destination, expected_account_id=DISPOSABLE,
            template_path=deploy / "database-secret-refresh-rehearsal.yaml",
            signers_path=deploy / "database-refresh-rehearsal-signers.json",
        )
        assert os.stat(destination).st_mode & 0o777 == 0o600
        assert "receipt.json" not in "\n".join(observed[0])
        assert "signature_base64" not in "\n".join(observed[1])


def test_ssm_failure_never_exposes_stdout_or_stderr():
    ssm = Mock()
    ssm.send_command.return_value = {"Command": {"CommandId": "command"}}
    ssm.get_command_invocation.return_value = {
        "Status": "Failed", "StandardOutputContent": "receipt-private-marker",
        "StandardErrorContent": "credential-private-marker",
    }
    with pytest.raises(RuntimeError) as error:
        handoff._command(ssm, "i-12345678", ["false"], "installed")
    assert "private-marker" not in str(error.value)


def test_rehearsal_requires_proof_for_handoff(monkeypatch):
    monkeypatch.setenv("RUN_DISPOSABLE_AWS_DATABASE_REFRESH_REHEARSAL", "1")
    with pytest.raises(RuntimeError, match="signed proof"):
        rehearsal.rehearse(
            expected_account=DISPOSABLE, production_account=PRODUCTION, region=REGION,
            vpc_id="vpc-test", private_subnet_id="subnet-1",
            second_private_subnet_id="subnet-2",
            stack_name=f"up-db-refresh-rehearsal-{RUN}",
            opt_in=True, handoff_instance_id="i-12345678",
        )