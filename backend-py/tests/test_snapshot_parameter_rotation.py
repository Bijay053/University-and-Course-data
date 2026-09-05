"""Security and transaction contracts for routine snapshot rotation."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import textwrap
from unittest.mock import patch


DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy"
sys.path.insert(0, str(DEPLOY_DIR))
SPEC = importlib.util.spec_from_file_location(
    "snapshot_rotation",
    DEPLOY_DIR / "rotate_snapshot_storage_via_parameter_store.py",
)
assert SPEC and SPEC.loader
rotation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rotation)


class _MissingInvocation(Exception):
    pass


class FakeSSM:
    class exceptions:
        InvocationDoesNotExist = _MissingInvocation

    def __init__(self, *, fail_command: bool = False) -> None:
        self.puts: list[dict[str, object]] = []
        self.commands: list[dict[str, object]] = []
        self.fail_command = fail_command

    def put_parameter(self, **kwargs):
        self.puts.append(kwargs)
        return {"Version": 7}

    def send_command(self, **kwargs):
        self.commands.append(kwargs)
        return {"Command": {"CommandId": "rotation-test"}}

    def get_command_invocation(self, **_kwargs):
        if self.fail_command:
            return {"Status": "Failed", "StandardErrorContent": "secret-ish error"}
        return {
            "Status": "Success",
            "StandardOutputContent": "rotated-and-roundtrip-verified\n",
        }


class FakeSession:
    def __init__(self, ssm: FakeSSM) -> None:
        self.ssm = ssm

    def client(self, name: str, region_name: str):
        assert name == "ssm"
        assert region_name == "ap-south-1"
        return self.ssm


def _secrets(*, endpoint: str | None = None) -> dict[str, str]:
    result = {
        "AWS_S3_BUCKET_NAME": "private-bucket",
        "AWS_S3_REGION": "ap-south-1",
        "AWS_ACCESS_KEY_ID": "private-access-key",
        "AWS_SECRET_ACCESS_KEY": 'private-$()-`secret`-\\\\-"value',
    }
    if endpoint is not None:
        result["AWS_S3_ENDPOINT_URL"] = endpoint
    return result


def test_configuration_is_one_versioned_bundle_and_clears_old_endpoint():
    with patch.dict(rotation.os.environ, _secrets(), clear=True):
        config = json.loads(rotation._configuration_from_environment())

    assert config == {
        "schema_version": 1,
        "bucket_name": "private-bucket",
        "region": "ap-south-1",
        "access_key_id": "private-access-key",
        "secret_access_key": 'private-$()-`secret`-\\\\-"value',
        "endpoint_url": "",
    }


def test_configuration_rejects_missing_and_newline_values():
    for key in (
        "AWS_S3_BUCKET_NAME",
        "AWS_S3_REGION",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    ):
        values = _secrets()
        del values[key]
        with patch.dict(rotation.os.environ, values, clear=True):
            try:
                rotation._configuration_from_environment()
            except SystemExit as exc:
                assert key in str(exc)
            else:
                raise AssertionError(f"{key} must be required")

    with patch.dict(
        rotation.os.environ,
        _secrets(endpoint="https://storage.example.test/\nleak"),
        clear=True,
    ):
        try:
            rotation._configuration_from_environment()
        except SystemExit as exc:
            assert "newline" in str(exc)
        else:
            raise AssertionError("newlines must fail closed")


def test_rotation_keeps_plaintext_out_of_command_history_and_output(capsys):
    ssm = FakeSSM()
    values = _secrets(endpoint="https://storage.example.test")
    with (
        patch.object(rotation, "_session", return_value=FakeSession(ssm)),
        patch.object(rotation.time, "sleep"),
        patch.dict(rotation.os.environ, values, clear=True),
    ):
        rotation.rotate("i-production", "ap-south-1", "alias/test")

    assert len(ssm.puts) == 1
    put = ssm.puts[0]
    assert put["Name"] == rotation.CONFIGURATION_PARAMETER
    assert put["Type"] == "SecureString"
    assert put["Overwrite"] is True
    assert put["KeyId"] == "alias/test"
    serialized_commands = repr(ssm.commands)
    output = capsys.readouterr().out
    for value in values.values():
        assert value not in serialized_commands
        assert value not in output
    assert ssm.commands == [{
        "InstanceIds": ["i-production"],
        "DocumentName": rotation.ROTATION_DOCUMENT,
        "Comment": "Rotate university portal snapshot storage",
        "Parameters": {"ExpectedVersion": ["7"]},
    }]
    assert "AWS-RunShellScript" not in serialized_commands


def test_document_failure_is_sanitized():
    ssm = FakeSSM(fail_command=True)
    with (
        patch.object(rotation, "_session", return_value=FakeSession(ssm)),
        patch.object(rotation.time, "sleep"),
        patch.dict(rotation.os.environ, _secrets(), clear=True),
    ):
        try:
            rotation.rotate("i-production", "ap-south-1", "alias/test")
        except RuntimeError as exc:
            assert "Failed" in str(exc)
            assert "secret-ish" not in str(exc)
        else:
            raise AssertionError("failed host transaction must fail rotation")


def test_template_is_scoped_transactional_and_syntactically_valid():
    template = (DEPLOY_DIR / "snapshot-parameter-store-iam.yaml").read_text()
    parameter_arn = "parameter/university-portal/snapshot-storage/configuration"
    assert "EnableKeyRotation: true" in template
    assert template.count(parameter_arn) == 4
    assert "Action: ssm:PutParameter" in template
    assert "Action: ssm:GetParameter" in template
    assert "ssm:GetParametersByPath" not in template
    assert "ssm:GetParameterHistory" not in template
    assert "AWS-RunShellScript" not in template
    assert "Type: AWS::SSM::Document" in template
    assert "ExpectedVersion:" in template
    assert 'allowedPattern: "^[1-9][0-9]*$"' in template
    assert 'expected_version="{{ ExpectedVersion }}"' in template
    assert "flock -x 9" in template
    assert 'Name=f"{name}:{expected_version}"' in template
    assert "trap rollback_on_error EXIT" in template
    assert "systemctl restart uni-api-py uni-celery" in template
    assert 'for unit in ("uni-api-py", "uni-celery")' in template
    assert template.count("shlex.split(raw_line, comments=False, posix=True)") == 2
    assert 'Name="/university-portal/snapshot-storage/configuration"' in template
    assert "run_storage_canary(timeout_seconds=30)" in template
    assert "rotated-and-roundtrip-verified" in template
    assert "SNAPSHOT_ENABLED" in template
    assert '("AWS_S3_ENDPOINT_URL", "endpoint_url")' in template

    literal = template.split("                - |\n", 1)[1]
    literal = literal.split("\n\n  DeploymentRotationPolicy:", 1)[0]
    script = textwrap.dedent(literal)
    syntax = subprocess.run(
        ["bash", "-n"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr
    heredocs = re.findall(
        r"<<'([A-Z_]+)'\n(.*?)\n\s*\1",
        script,
        flags=re.DOTALL,
    )
    assert {marker for marker, _source in heredocs} == {
        "PY_VERIFY",
        "PY_CONFIG",
        "PY_SMOKE",
    }
    for marker, source in heredocs:
        compile(textwrap.dedent(source), f"<snapshot-rotation-{marker}>", "exec")