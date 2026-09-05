#!/usr/bin/env python3
"""Install production snapshot-storage credentials through encrypted SSM.

The production host creates the temporary private key. Only its public
certificate and a CMS-encrypted environment payload pass through SSM command
input/output, so plaintext values never enter Git, argv, shell history, or SSM
Run Command history.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import inspect
import os
import shlex
import textwrap
import uuid

from install_openai_fallback_via_ssm import (
    _encrypt_with_certificate,
    _run_command,
    _session,
)


ENV_PATH = "/etc/university-portal/snapshot-storage.env"
DROPIN_NAME = "99-snapshot-storage.conf"
SERVICES = ("uni-api-py", "uni-celery")
REQUIRED_KEYS = (
    "AWS_S3_BUCKET_NAME",
    "AWS_S3_REGION",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
)
OPTIONAL_KEYS = ("AWS_S3_ENDPOINT_URL",)


def _python_from_cmdline(raw_cmdline: bytes) -> str:
    """Return the direct interpreter argv[0] used by a systemd service."""
    first = raw_cmdline.split(b"\0", 1)[0]
    if not first:
        raise ValueError("service process has an empty command line")
    return first.decode()


def restore_lifecycle_configuration(client, bucket: str, configuration: dict) -> None:
    """Restore the full supported lifecycle shape at the correct API levels."""
    config = dict(configuration)
    transition_default = config.pop("TransitionDefaultMinimumObjectSize", None)
    if set(config) != {"Rules"}:
        raise ValueError("unexpected lifecycle configuration fields")
    options = {
        "Bucket": bucket,
        "LifecycleConfiguration": config,
    }
    if transition_default is not None:
        options["TransitionDefaultMinimumObjectSize"] = transition_default
    client.put_bucket_lifecycle_configuration(**options)


def _environment_payload() -> bytes:
    values = {
        key: os.environ.get(key)
        for key in (*REQUIRED_KEYS, *OPTIONAL_KEYS)
    }
    missing = [key for key in REQUIRED_KEYS if not values[key]]
    if missing:
        raise SystemExit(
            "Missing required snapshot-storage secrets: " + ", ".join(missing)
        )

    lines: list[str] = []
    for key, value in values.items():
        if value is None or value == "":
            continue
        if "\n" in value or "\r" in value:
            raise SystemExit(f"{key} must not contain a newline")
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key}="{escaped}"')
    lines.append('SNAPSHOT_ENABLED="true"')
    return ("\n".join(lines) + "\n").encode()


async def _delete_smoke_snapshot(snapshot_store, key: str) -> None:
    session = snapshot_store._make_async_session()
    extra = {}
    endpoint = os.environ.get("AWS_S3_ENDPOINT_URL")
    if endpoint:
        extra["endpoint_url"] = endpoint
    bucket = os.environ["AWS_S3_BUCKET_NAME"]
    for attempt in range(3):
        try:
            async with session.client("s3", **extra) as s3:
                try:
                    head = await s3.head_object(Bucket=bucket, Key=key)
                except Exception as exc:
                    status = getattr(exc, "response", {}).get(
                        "ResponseMetadata", {}
                    ).get("HTTPStatusCode")
                    code = str(
                        getattr(exc, "response", {}).get("Error", {}).get("Code", "")
                    )
                    if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
                        return
                    raise
                version_id = head.get("VersionId")
                delete_options = {"Bucket": bucket, "Key": key}
                if version_id:
                    delete_options["VersionId"] = version_id
                await s3.delete_object(**delete_options)
                try:
                    await s3.head_object(Bucket=bucket, Key=key)
                except Exception as exc:
                    status = getattr(exc, "response", {}).get(
                        "ResponseMetadata", {}
                    ).get("HTTPStatusCode")
                    code = str(
                        getattr(exc, "response", {}).get("Error", {}).get("Code", "")
                    )
                    assert status == 404 or code in {
                        "404", "NoSuchKey", "NotFound"
                    }
                else:
                    raise AssertionError(
                        "smoke-test snapshot still exists after deletion"
                    )
            return
        except Exception:
            if attempt == 2:
                raise
            await asyncio.sleep(2 ** attempt)


async def snapshot_round_trip(snapshot_store=None) -> None:
    """Save, retrieve, and permanently remove one deterministic smoke object."""
    if snapshot_store is None:
        from app.services import snapshot_store as snapshot_store_module

        snapshot_store = snapshot_store_module

    assert snapshot_store.is_enabled()
    marker = b"university-portal snapshot storage smoke test"
    job_id = "deployment-smoke-" + uuid.uuid4().hex
    url = "https://snapshot-smoke.invalid/test"
    key = snapshot_store.build_s3_key(0, job_id, url, "html")
    try:
        uploaded_key = await snapshot_store.upload_snapshot(
            marker,
            university_id=0,
            scrape_job_id=job_id,
            url=url,
            snapshot_type="html",
            content_type="text/plain; charset=utf-8",
        )
        assert uploaded_key == key
        downloaded = await snapshot_store.download_snapshot(key)
        assert downloaded == marker
    finally:
        # PutObject can commit before a response error is surfaced. Always
        # clean the deterministic key even when upload_snapshot returns None.
        await _delete_smoke_snapshot(snapshot_store, key)


def _install_script(
    *,
    encrypted_b64: str,
    key_path: str,
    cert_path: str,
) -> str:
    env_path = shlex.quote(ENV_PATH)
    required_literal = repr(REQUIRED_KEYS)
    allowed_literal = repr((*REQUIRED_KEYS, *OPTIONAL_KEYS, "SNAPSHOT_ENABLED"))
    services = " ".join(SERVICES)
    restore_helper = textwrap.indent(
        inspect.getsource(restore_lifecycle_configuration),
        "        ",
    )
    smoke_helpers = textwrap.indent(
        inspect.getsource(_delete_smoke_snapshot)
        + "\n\n"
        + inspect.getsource(snapshot_round_trip),
        "        ",
    )
    return textwrap.dedent(
        f"""\
        set -eu
        set +x
        umask 077
        env_path={env_path}
        env_backup="${{env_path}}.pre-install"
        env_absent="${{env_path}}.pre-install-absent"
        lifecycle_backup=/etc/university-portal/snapshot-lifecycle.pre-install.json
        lifecycle_absent=/etc/university-portal/snapshot-lifecycle.pre-install-absent
        tmp=

        workdir=$(systemctl show uni-api-py --property=WorkingDirectory --value)
        api_pid=$(systemctl show uni-api-py --property=MainPID --value)
        test -n "$workdir"
        test -n "$api_pid" && test "$api_pid" != 0
        python=$(python3 - "$api_pid" <<'PYTHON_COMMAND'
        import sys
        print(open(f"/proc/{{sys.argv[1]}}/cmdline", "rb").read().split(b"\\0", 1)[0].decode())
        PYTHON_COMMAND
        )
        test -x "$python"
        "$python" -c 'import sys; assert sys.version_info.major == 3'

        restore_lifecycle() {{
          if test -f "$lifecycle_backup" || test -f "$lifecycle_absent"; then
            "$python" - "$env_path" "$lifecycle_backup" "$lifecycle_absent" <<'PYLIFECYCLE_RESTORE'
        import json
        import os
        import shlex
        import sys
        import time

{restore_helper}
        env_path, backup_path, absent_path = sys.argv[1:]
        with open(env_path, encoding="utf-8") as stream:
            for raw_line in stream:
                parsed = shlex.split(raw_line, comments=False, posix=True)
                key, value = parsed[0].split("=", 1)
                os.environ[key] = value
        import boto3
        client = boto3.client(
            "s3",
            region_name=os.environ["AWS_S3_REGION"],
            aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
            **({{"endpoint_url": os.environ["AWS_S3_ENDPOINT_URL"]}}
               if os.environ.get("AWS_S3_ENDPOINT_URL") else {{}}),
        )
        bucket = os.environ["AWS_S3_BUCKET_NAME"]
        for attempt in range(3):
            try:
                if os.path.exists(backup_path):
                    with open(backup_path, encoding="utf-8") as stream:
                        config = json.load(stream)
                    restore_lifecycle_configuration(client, bucket, config)
                elif os.path.exists(absent_path):
                    client.delete_bucket_lifecycle(Bucket=bucket)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        PYLIFECYCLE_RESTORE
            return $?
          fi
          return 0
        }}

        restore_previous() {{
          lifecycle_failed=0
          config_failed=0
          services_failed=0
          restore_lifecycle || lifecycle_failed=1
          if test -f "$env_backup"; then
            mv -f "$env_backup" "$env_path" || config_failed=1
          elif test -f "$env_absent"; then
            rm -f "$env_path" "$env_absent" || config_failed=1
          fi
          for service in {services}; do
            dropin="/etc/systemd/system/${{service}}.service.d/{DROPIN_NAME}"
            backup="${{dropin}}.pre-install"
            absent="${{dropin}}.pre-install-absent"
            if test -f "$backup"; then
              mv -f "$backup" "$dropin" || config_failed=1
            elif test -f "$absent"; then
              rm -f "$dropin" "$absent" || config_failed=1
            fi
          done
          systemctl daemon-reload || services_failed=1
          systemctl restart {services} || services_failed=1
          sleep 8
          systemctl is-active --quiet {services} || services_failed=1
          rm -f "$lifecycle_backup" "$lifecycle_absent"
          if test "$lifecycle_failed" -ne 0 ||
             test "$config_failed" -ne 0 ||
             test "$services_failed" -ne 0; then
            return 1
          fi
          return 0
        }}

        rollback_on_error() {{
          rc=$?
          trap - EXIT
          test -z "$tmp" || rm -f "$tmp"
          rm -f {shlex.quote(key_path)} {shlex.quote(cert_path)}
          if ! restore_previous; then
            echo "snapshot-storage install and rollback failed; manual recovery required" >&2
          fi
          exit "$rc"
        }}

        install -d -m 700 /etc/university-portal
        rm -f "$env_backup" "$env_absent"
        if test -f "$env_path"; then
          cp -p "$env_path" "$env_backup"
        else
          touch "$env_absent"
        fi
        for service in {services}; do
          dir="/etc/systemd/system/${{service}}.service.d"
          dropin="${{dir}}/{DROPIN_NAME}"
          install -d -m 755 "$dir"
          rm -f "${{dropin}}.pre-install" "${{dropin}}.pre-install-absent"
          if test -f "$dropin"; then
            cp -p "$dropin" "${{dropin}}.pre-install"
          else
            touch "${{dropin}}.pre-install-absent"
          fi
        done
        trap rollback_on_error EXIT

        tmp=$(mktemp "${{env_path}}.XXXXXX")
        printf %s {shlex.quote(encrypted_b64)} | base64 -d |
          openssl cms -decrypt -binary -inform DER \
            -recip {shlex.quote(cert_path)} -inkey {shlex.quote(key_path)} >"$tmp"
        python3 - "$tmp" <<'PYVALIDATE'
        import shlex
        import sys

        required = set({required_literal})
        allowed = set({allowed_literal})
        values = {{}}
        with open(sys.argv[1], encoding="utf-8") as stream:
            for raw_line in stream:
                parsed = shlex.split(raw_line, comments=False, posix=True)
                assert len(parsed) == 1 and "=" in parsed[0]
                key, value = parsed[0].split("=", 1)
                assert key not in values
                values[key] = value
        assert set(values).issubset(allowed)
        assert required.issubset(values)
        assert all(values[key] for key in required)
        assert values.get("SNAPSHOT_ENABLED") == "true"
        PYVALIDATE
        chmod 600 "$tmp"
        mv -f "$tmp" "$env_path"
        tmp=
        rm -f {shlex.quote(key_path)} {shlex.quote(cert_path)}

        rm -f "$lifecycle_backup" "$lifecycle_absent"
        "$python" - "$env_path" "$lifecycle_backup" "$lifecycle_absent" <<'PYLIFECYCLE_BACKUP'
        import json
        import os
        import shlex
        import sys

        env_path, backup_path, absent_path = sys.argv[1:]
        with open(env_path, encoding="utf-8") as stream:
            for raw_line in stream:
                parsed = shlex.split(raw_line, comments=False, posix=True)
                key, value = parsed[0].split("=", 1)
                os.environ[key] = value
        import boto3
        from botocore.exceptions import ClientError
        client = boto3.client(
            "s3",
            region_name=os.environ["AWS_S3_REGION"],
            aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
            **({{"endpoint_url": os.environ["AWS_S3_ENDPOINT_URL"]}}
               if os.environ.get("AWS_S3_ENDPOINT_URL") else {{}}),
        )
        try:
            response = client.get_bucket_lifecycle_configuration(
                Bucket=os.environ["AWS_S3_BUCKET_NAME"],
            )
        except ClientError as exc:
            code = str(exc.response.get("Error", {{}}).get("Code", ""))
            if code != "NoSuchLifecycleConfiguration":
                raise
            open(absent_path, "x").close()
            os.chmod(absent_path, 0o600)
        else:
            configuration = {{
                key: response[key]
                for key in ("Rules", "TransitionDefaultMinimumObjectSize")
                if key in response
            }}
            with open(backup_path, "x", encoding="utf-8") as stream:
                json.dump(configuration, stream)
            os.chmod(backup_path, 0o600)
        PYLIFECYCLE_BACKUP

        for service in {services}; do
          dropin="/etc/systemd/system/${{service}}.service.d/{DROPIN_NAME}"
          printf '%s\\n' '[Service]' 'EnvironmentFile={ENV_PATH}' >"$dropin"
          chmod 644 "$dropin"
        done
        systemctl daemon-reload
        restart_since=$(date --iso-8601=seconds)
        systemctl restart {services}
        sleep 8
        systemctl is-active --quiet {services}
        curl --fail --silent --show-error http://127.0.0.1:8000/api/health >/dev/null

        python3 - "$env_path" <<'PYPROCESS'
        import shlex
        import subprocess
        import sys

        expected = {{}}
        with open(sys.argv[1], encoding="utf-8") as stream:
            for raw_line in stream:
                parsed = shlex.split(raw_line, comments=False, posix=True)
                key, value = parsed[0].split("=", 1)
                expected[key.encode()] = value.encode()
        for unit in ("uni-api-py", "uni-celery"):
            pid = subprocess.check_output(
                ["systemctl", "show", "--property=MainPID", "--value", unit],
                text=True,
            ).strip()
            assert pid and pid != "0"
            process_env = dict(
                item.split(b"=", 1)
                for item in open(f"/proc/{{pid}}/environ", "rb").read().split(b"\\0")
                if b"=" in item
            )
            for key, value in expected.items():
                assert process_env.get(key) == value
        PYPROCESS
        journalctl -u uni-api-py --since "$restart_since" --no-pager |
          grep -Fq "[SNAPSHOT] enabled"

        cd "$workdir"
        PYTHONPATH=. "$python" - "$env_path" <<'PYSMOKE'
        import asyncio
        import os
        import shlex
        import sys
        import uuid

        with open(sys.argv[1], encoding="utf-8") as stream:
            for raw_line in stream:
                parsed = shlex.split(raw_line, comments=False, posix=True)
                key, value = parsed[0].split("=", 1)
                os.environ[key] = value

{smoke_helpers}
        asyncio.run(snapshot_round_trip())
        PYSMOKE

        rm -f "$env_backup" "$env_absent" "$lifecycle_backup" "$lifecycle_absent"
        for service in {services}; do
          dropin="/etc/systemd/system/${{service}}.service.d/{DROPIN_NAME}"
          rm -f "${{dropin}}.pre-install" "${{dropin}}.pre-install-absent"
        done
        trap - EXIT
        echo installed-and-roundtrip-verified
        """
    )


def install(instance_id: str, region: str) -> None:
    ssm = _session().client("ssm", region_name=region)
    transfer_id = uuid.uuid4().hex
    remote_dir = "/root/.university-portal-secret-transfer"
    key_path = f"{remote_dir}/{transfer_id}.key.pem"
    cert_path = f"{remote_dir}/{transfer_id}.cert.pem"

    certificate_b64 = _run_command(
        ssm,
        instance_id,
        "Prepare encrypted university portal snapshot-storage transfer",
        [
            "set -eu",
            "umask 077",
            f"install -d -m 700 {shlex.quote(remote_dir)}",
            (
                "openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 1 "
                "-subj /CN=university-portal-snapshot-storage-transfer "
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
        output = _run_command(
            ssm,
            instance_id,
            "Install encrypted university portal snapshot-storage environment",
            [
                _install_script(
                    encrypted_b64=encrypted_b64,
                    key_path=key_path,
                    cert_path=cert_path,
                )
            ],
        )
        if output != "installed-and-roundtrip-verified":
            raise RuntimeError("Production installer returned an unexpected response")
    except Exception:
        _run_command(
            ssm,
            instance_id,
            "Clean up university portal snapshot-storage secret transfer",
            [f"rm -f {shlex.quote(key_path)} {shlex.quote(cert_path)}"],
        )
        raise

    print(
        "Snapshot storage installed; both services are active and the "
        "upload/download/delete smoke test passed."
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", default="ap-south-1")
    args = parser.parse_args()
    install(args.instance_id, args.region)


if __name__ == "__main__":
    main()