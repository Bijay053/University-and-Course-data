"""Security regression tests for routine OpenAI fallback rotation."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from unittest.mock import patch


DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy"
sys.path.insert(0, str(DEPLOY_DIR))
SPEC = importlib.util.spec_from_file_location(
    "openai_rotation", DEPLOY_DIR / "rotate_openai_fallback_via_parameter_store.py"
)
assert SPEC and SPEC.loader
rotation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rotation)


class _MissingInvocation(Exception):
    pass


class FakeSSM:
    class exceptions:
        InvocationDoesNotExist = _MissingInvocation

    def __init__(self, fail_command: bool = False) -> None:
        self.puts: list[dict[str, object]] = []
        self.commands: list[dict[str, object]] = []
        self.fail_command = fail_command

    def put_parameter(self, **kwargs):
        self.puts.append(kwargs)

    def send_command(self, **kwargs):
        self.commands.append(kwargs)
        return {"Command": {"CommandId": "rotation-test"}}

    def get_command_invocation(self, **kwargs):
        if self.fail_command:
            return {
                "Status": "Failed",
                "StandardErrorContent": "simulated transactional rotation failure",
            }
        return {"Status": "Success", "StandardOutputContent": "rotated\n"}


class FakeSession:
    def __init__(self, ssm: FakeSSM) -> None:
        self.ssm = ssm

    def client(self, name: str, region_name: str):
        assert name == "ssm"
        return self.ssm


def test_rotation_keeps_plaintext_out_of_command_history_and_output(capsys) -> None:
    ssm = FakeSSM()
    api_key = "test-secret-api-key"
    base_url = "https://openai.example.test/v1"
    with (
        patch.object(rotation, "_session", return_value=FakeSession(ssm)),
        patch.object(rotation.time, "sleep"),
        patch.dict(
            rotation.os.environ,
            {"OPENAI_API_KEY": api_key, "OPENAI_BASE_URL": base_url},
            clear=True,
        ),
    ):
        rotation.rotate("i-production", "ap-south-1", "alias/test")

    assert [call["Name"] for call in ssm.puts] == [rotation.CONFIGURATION_PARAMETER]
    assert all(call["Type"] == "SecureString" for call in ssm.puts)
    serialized_command = repr(ssm.commands)
    assert api_key not in serialized_command
    assert base_url not in serialized_command
    assert api_key not in capsys.readouterr().out
    assert all(call["DocumentName"] == rotation.ROTATION_DOCUMENT for call in ssm.commands)
    assert all("Parameters" not in call for call in ssm.commands)
    assert "AWS-RunShellScript" not in serialized_command


def test_document_failure_is_reported_after_transactional_host_rollback() -> None:
    ssm = FakeSSM(fail_command=True)
    with (
        patch.object(rotation, "_session", return_value=FakeSession(ssm)),
        patch.object(rotation.time, "sleep"),
        patch.dict(rotation.os.environ, {"OPENAI_API_KEY": "secret"}, clear=True),
    ):
        try:
            rotation.rotate("i-production", "ap-south-1", "alias/test")
        except RuntimeError as exc:
            assert "simulated transactional rotation failure" in str(exc)
        else:
            raise AssertionError("rotation should fail when the host transaction fails")

    assert len(ssm.commands) == 1


def test_iam_template_scopes_parameter_access_and_uses_customer_key() -> None:
    template = (DEPLOY_DIR / "openai-parameter-store-iam.yaml").read_text()
    assert "EnableKeyRotation: true" in template
    assert "Action: ssm:PutParameter" in template
    assert "Action: ssm:GetParameter" in template
    assert "parameter/university-portal/openai/configuration" in template
    assert "parameter/university-portal/openai/active" not in template
    assert "parameter/university-portal/openai/candidate" not in template
    assert "AWS-RunShellScript" not in template
    assert "Type: AWS::SSM::Document" in template
    assert 'region="{{ global:REGION }}"' in template
    assert 'boto3.client("ssm", region_name=sys.argv[2])' in template
    assert "systemctl restart uni-api-py uni-celery" in template
    assert 'for unit in ("uni-api-py", "uni-celery")' in template
    assert "trap rollback_on_error EXIT" in template
    assert "ssm:GetParametersByPath" not in template
    assert "ssm:GetParameters\n" not in template
    assert "ssm:GetParameterHistory" not in template