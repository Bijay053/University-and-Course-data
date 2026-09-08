"""Security, transaction, and TLS contracts for RDS credential refresh."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import subprocess
import sys
import textwrap
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = BACKEND_ROOT / "deploy"
sys.path.insert(0, str(DEPLOY_DIR))
SPEC = importlib.util.spec_from_file_location(
    "database_refresh",
    DEPLOY_DIR / "refresh_database_credentials_via_secrets_manager.py",
)
assert SPEC and SPEC.loader
refresh_client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(refresh_client)


class _MissingInvocation(Exception):
    pass


class _SSM:
    class exceptions:
        InvocationDoesNotExist = _MissingInvocation

    def __init__(self, status: str = "Success") -> None:
        self.status = status
        self.commands: list[dict[str, object]] = []

    def send_command(self, **kwargs):
        self.commands.append(kwargs)
        return {"Command": {"CommandId": "refresh-test"}}

    def get_command_invocation(self, **_kwargs):
        return {
            "Status": self.status,
            "StandardOutputContent": refresh_client.SUCCESS_OUTPUT + "\n",
        }


class _Session:
    def __init__(self, ssm: _SSM) -> None:
        self.ssm = ssm

    def client(self, name: str, region_name: str):
        assert name == "ssm"
        assert region_name == "ap-south-1"
        return self.ssm


def test_refresh_client_has_no_secret_input_or_command_parameter(capsys) -> None:
    ssm = _SSM()
    with (
        patch.object(refresh_client, "_session", return_value=_Session(ssm)),
        patch.object(refresh_client.time, "sleep"),
    ):
        refresh_client.refresh("i-production", "ap-south-1")

    assert ssm.commands == [{
        "InstanceIds": ["i-production"],
        "DocumentName": "university-portal-database-credential-refresh",
        "Comment": "Refresh university portal RDS database credentials",
    }]
    assert "password" not in repr(ssm.commands).lower()
    assert "password" not in capsys.readouterr().out.lower()


def test_refresh_client_failure_is_sanitized() -> None:
    ssm = _SSM(status="Failed")
    with (
        patch.object(refresh_client, "_session", return_value=_Session(ssm)),
        patch.object(refresh_client.time, "sleep"),
    ):
        try:
            refresh_client.refresh("i-production", "ap-south-1")
        except RuntimeError as exc:
            assert "Failed" in str(exc)
            assert "StandardErrorContent" not in str(exc)
        else:
            raise AssertionError("host failure must fail refresh")


def test_template_limits_secret_read_and_host_transaction_is_valid() -> None:
    template = (DEPLOY_DIR / "database-secret-rotation-iam.yaml").read_text()
    assert "python=/opt/university-portal/backend-py/.venv/bin/python" in template
    assert "backend-py/venv/bin/python" not in template
    assert "DatabaseSecretArn:" in template
    assert "ScheduleState:" in template
    assert "State: !Ref ScheduleState" in template
    assert "[A-Za-z0-9/_+=.@!-]+" in template
    assert "Action: secretsmanager:GetSecretValue" in template
    assert "Resource: !Ref DatabaseSecretArn" in template
    assert "secretsmanager:GetSecretValue" in template
    assert "secretsmanager:GetSecretValue\n            Resource: \"*\"" not in template
    assert "secretsmanager:ListSecrets" not in template
    deployment_policy = template.split("DeploymentRefreshPolicy:", 1)[1].split(
        "ProductionDatabaseSecretReadPolicy:", 1
    )[0]
    assert "secretsmanager:GetSecretValue" not in deployment_policy
    assert "Action: ssm:SendCommand" in template
    assert "AWS-RunShellScript" not in template
    assert "trap rollback_on_error EXIT" in template
    assert "systemctl cat \"$unit\"" in template
    assert "managed database credentials" in template
    assert "<<'PY_BOOTSTRAP'" in template
    assert 'process_env[b"DATABASE_URL"]' in template
    assert template.index("systemctl cat \"$unit\"") < template.index("rm -f \"$backup_path\"")
    assert template.index("PY_BOOTSTRAP") < template.index("trap rollback_on_error EXIT")
    assert "systemctl restart uni-api-py uni-celery" in template
    assert 'text("SELECT 1")' in template
    assert 'timeoutSeconds: "300"' in template
    assert template.count("curl --connect-timeout 2 --max-time 10") == 1
    assert template.count("timeout 60s systemctl restart uni-api-py uni-celery") == 1
    assert "control cancel_consumer scrape" in template
    assert "ScrapeRuntimeJob.status == \"running\"" in template
    assert "control add_consumer scrape" in template
    assert template.index("pause_and_verify_no_running_scrapes") < template.index(
        'mv -f "$tmp" "$env_path"'
    )
    assert "connect_args.update(timeout=10, command_timeout=10)" in template
    assert "asyncio.wait_for(smoke(), timeout=20)" in template
    assert "set +x" in template
    assert "get_secret_value(" in template
    assert "SecretId=sys.argv[3]" in template
    assert "region='${AWS::Region}'" in template
    assert "{{ global:REGION }}" not in template
    assert 'required = {"username", "password"}' in template
    assert 'config.get("dbname")' in template
    assert 'config.get("host") or existing_url.hostname' in template
    assert 'config.get("port") or existing_url.port or 5432' in template
    assert 'unquote(existing_url.path.lstrip("/"))' in template
    assert '"$backup_path" <<\'PY_CONFIG\'' in template
    assert 'DATABASE_SECRET_VERSION="{version_id}"' in template
    assert 'DATABASE_REQUIRE_TLS="true"' in template
    assert 'cmp -s "$tmp" "$env_path"' in template
    assert "Type: AWS::Scheduler::Schedule" in template
    assert "ScheduleExpression: rate(5 minutes)" in template
    assert "Arn: arn:aws:scheduler:::aws-sdk:ssm:sendCommand" in template
    assert "Service: scheduler.amazonaws.com" in template
    assert "aws:SourceAccount: !Ref AWS::AccountId" in template
    assert "Type: AWS::Scheduler::ScheduleGroup" in template
    assert "aws:SourceArn: !GetAtt DatabaseCredentialRefreshScheduleGroup.Arn" in template
    assert "GroupName: !Ref DatabaseCredentialRefreshScheduleGroup" in template
    assert "RoleArn: !GetAtt ScheduledRefreshRole.Arn" in template
    assert "ApplyOnlyAtCronInterval" not in template

    literal = template.split("                - !Sub |\n", 1)[1]
    literal = literal.split("\n\n  ScheduledRefreshRole:", 1)[0]
    script = textwrap.dedent(literal)
    syntax = subprocess.run(
        ["bash", "-n"], input=script, text=True, capture_output=True, check=False
    )
    assert syntax.returncode == 0, syntax.stderr
    heredocs = re.findall(r"<<'([A-Z_]+)'\n(.*?)\n\s*\1", script, re.DOTALL)
    assert {name for name, _ in heredocs} == {
        "PY_VERIFY", "PY_IDLE", "PY_BOOTSTRAP", "PY_CONFIG", "PY_SMOKE",
    }
    for name, source in heredocs:
        compile(textwrap.dedent(source), f"<database-refresh-{name}>", "exec")


def test_every_database_engine_uses_certificate_verifying_tls() -> None:
    database_source = (BACKEND_ROOT / "app" / "database.py").read_text()
    celery_source = (BACKEND_ROOT / "app" / "tasks" / "celery_app.py").read_text()
    assert "ssl.create_default_context()" in database_source
    assert "if not settings.database_require_tls:" in database_source
    assert "connect_args=postgres_tls_connect_args()" in database_source
    assert celery_source.count("connect_args=postgres_tls_connect_args()") == 2
    assert "sslmode" in (BACKEND_ROOT / "app" / "config.py").read_text()


def test_services_load_generated_database_environment_last() -> None:
    for service in ("uni-api-py.service", "uni-celery.service"):
        source = (DEPLOY_DIR / service).read_text()
        assert "ExecStart=/opt/university-portal/backend-py/.venv/bin/" in source
        assert "backend-py/venv/bin/" not in source
        files = [
            line.split("=", 1)[1]
            for line in source.splitlines()
            if line.startswith("EnvironmentFile=")
        ]
        assert files[-1] == "/etc/university-portal/database.env"