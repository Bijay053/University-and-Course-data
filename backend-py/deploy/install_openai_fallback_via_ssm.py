#!/usr/bin/env python3
"""Install the OpenAI fallback environment through envelope-encrypted SSM.

Only a temporary public certificate and encrypted CMS payload pass through SSM
command input/output. Plaintext credentials never appear in command history.
"""
from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import uuid

import boto3


ENV_PATH = "/etc/university-portal/openai.env"
SERVICES = ("uni-api-py", "uni-celery")


def _session() -> boto3.Session:
    access_key = os.environ.get("AWS_SSM_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SSM_SECRET_ACCESS_KEY")
    if bool(access_key) != bool(secret_key):
        raise SystemExit(
            "AWS_SSM_ACCESS_KEY_ID and AWS_SSM_SECRET_ACCESS_KEY must be set together"
        )
    if access_key and secret_key:
        return boto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
    return boto3.Session()


def _run_command(ssm, instance_id: str, comment: str, commands: list[str]) -> str:
    response = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Comment=comment,
        Parameters={"commands": commands},
    )
    command_id = response["Command"]["CommandId"]
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            result = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
            )
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if result["Status"] in {"Pending", "InProgress", "Delayed"}:
            continue
        if result["Status"] != "Success":
            stderr = result.get("StandardErrorContent", "").strip()
            raise RuntimeError(
                f"SSM command {comment!r} failed with {result['Status']}: {stderr}"
            )
        return result.get("StandardOutputContent", "").strip()
    raise TimeoutError(f"SSM command {comment!r} did not finish within 180 seconds")


def _environment_payload() -> bytes:
    direct_api_key = os.environ.get("OPENAI_API_KEY")
    integration_api_key = os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY")
    if direct_api_key:
        api_key = direct_api_key
        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    else:
        api_key = integration_api_key
        base_url = os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL")

    values = {
        "AI_INTEGRATIONS_OPENAI_API_KEY": api_key,
        "AI_INTEGRATIONS_OPENAI_BASE_URL": base_url,
    }
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise SystemExit(
            "Set OPENAI_API_KEY for external production, or provide both "
            "AI_INTEGRATIONS_OPENAI_API_KEY and AI_INTEGRATIONS_OPENAI_BASE_URL"
        )

    lines: list[str] = []
    for key, value in values.items():
        assert value is not None
        if "\n" in value or "\r" in value:
            raise SystemExit(f"{key} must not contain a newline")
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key}="{escaped}"')
    return ("\n".join(lines) + "\n").encode()


def _encrypt_with_certificate(certificate_pem: str, payload: bytes) -> str:
    with tempfile.TemporaryDirectory() as temp_dir:
        certificate_path = Path(temp_dir) / "recipient.pem"
        certificate_path.write_text(certificate_pem, encoding="ascii")
        encrypted = subprocess.run(
            [
                "openssl",
                "cms",
                "-encrypt",
                "-binary",
                "-aes-256-cbc",
                "-outform",
                "DER",
                str(certificate_path),
            ],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout
    return base64.b64encode(encrypted).decode("ascii")


def install(instance_id: str, region: str) -> None:
    ssm = _session().client("ssm", region_name=region)
    transfer_id = uuid.uuid4().hex
    remote_dir = "/root/.university-portal-secret-transfer"
    key_path = f"{remote_dir}/{transfer_id}.key.pem"
    cert_path = f"{remote_dir}/{transfer_id}.cert.pem"

    certificate_b64 = _run_command(
        ssm,
        instance_id,
        "Prepare university portal encrypted secret transfer",
        [
            "set -eu",
            "umask 077",
            f"install -d -m 700 {shlex.quote(remote_dir)}",
            (
                "openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 1 "
                f"-subj /CN=university-portal-secret-transfer "
                f"-keyout {shlex.quote(key_path)} -out {shlex.quote(cert_path)} "
                ">/dev/null 2>&1"
            ),
            f"base64 -w0 {shlex.quote(cert_path)}",
        ],
    )
    try:
        certificate_pem = base64.b64decode(certificate_b64).decode("ascii")
        encrypted_b64 = _encrypt_with_certificate(
            certificate_pem,
            _environment_payload(),
        )
        env_path = shlex.quote(ENV_PATH)
        install_commands = [
            "set -eu",
            "umask 077",
            "install -d -m 700 /etc/university-portal",
            f"tmp=$(mktemp {env_path}.XXXXXX)",
            (
                f"printf %s {shlex.quote(encrypted_b64)} | base64 -d | "
                "openssl cms -decrypt -binary -inform DER "
                f"-recip {shlex.quote(cert_path)} -inkey {shlex.quote(key_path)} "
                '>"$tmp"'
            ),
            (
                'grep -q "^AI_INTEGRATIONS_OPENAI_API_KEY=" "$tmp" && '
                'grep -q "^AI_INTEGRATIONS_OPENAI_BASE_URL=" "$tmp"'
            ),
            'chmod 600 "$tmp"',
            f"mv -f \"$tmp\" {env_path}",
            f"rm -f {shlex.quote(key_path)} {shlex.quote(cert_path)}",
        ]
        for service in SERVICES:
            dropin_dir = f"/etc/systemd/system/{service}.service.d"
            install_commands.extend(
                [
                    f"install -d -m 755 {shlex.quote(dropin_dir)}",
                    (
                        "printf '%s\\n' '[Service]' "
                        f"'EnvironmentFile={ENV_PATH}' "
                        f"> {shlex.quote(dropin_dir + '/99-openai-fallback.conf')}"
                    ),
                    f"chmod 644 {shlex.quote(dropin_dir + '/99-openai-fallback.conf')}",
                ]
            )
        install_commands.extend(
            [
                "systemctl daemon-reload",
                f"systemctl restart {' '.join(SERVICES)}",
                f"systemctl is-active --quiet {' '.join(SERVICES)}",
                "echo installed",
            ]
        )
        output = _run_command(
            ssm,
            instance_id,
            "Install university portal encrypted OpenAI fallback environment",
            install_commands,
        )
        if output != "installed":
            raise RuntimeError("Production installer returned an unexpected response")
    except Exception:
        _run_command(
            ssm,
            instance_id,
            "Clean up university portal encrypted secret transfer",
            [f"rm -f {shlex.quote(key_path)} {shlex.quote(cert_path)}"],
        )
        raise

    print("OpenAI fallback environment installed; both services are active.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", default="ap-south-1")
    args = parser.parse_args()
    install(args.instance_id, args.region)


if __name__ == "__main__":
    main()