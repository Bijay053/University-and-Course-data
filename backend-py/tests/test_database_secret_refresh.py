"""Security, transaction, and TLS contracts for RDS credential refresh."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap
import types
from unittest.mock import patch

from botocore.exceptions import ClientError
import pytest


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

ALERT_SPEC = importlib.util.spec_from_file_location(
    "database_refresh_alert",
    DEPLOY_DIR / "prove_database_refresh_alert.py",
)
assert ALERT_SPEC and ALERT_SPEC.loader
alert_client = importlib.util.module_from_spec(ALERT_SPEC)
ALERT_SPEC.loader.exec_module(alert_client)


def _notifier_source() -> str:
    template = (DEPLOY_DIR / "database-secret-rotation-iam.yaml").read_text()
    source = template.split("ZipFile: |\n", 1)[1].split(
        "\n\n  DatabaseRefreshFailureRule:", 1
    )[0]
    return textwrap.dedent(source)


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
    assert "zz-database-credentials.conf" in template
    assert (
        "'EnvironmentFile=/etc/university-portal/database.env'" in template
    )
    assert "systemctl daemon-reload" in template
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
    assert "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem" in template
    assert "system-and-rds-ca-bundle.pem" in template
    assert 'SSL_CERT_FILE="/etc/university-portal/' in template
    assert "ssl.create_default_context(cafile=sys.argv[1])" in template
    assert "CERT_NONE" not in template
    assert "check_hostname = False" not in template
    assert 'cmp -s "$tmp" "$env_path"' in template
    assert "Type: AWS::Scheduler::Schedule" in template
    assert "ScheduleExpression: rate(5 minutes)" in template
    assert "Arn: arn:aws:scheduler:::aws-sdk:ssm:sendCommand" in template
    assert "Service: scheduler.amazonaws.com" in template
    assert "aws:SourceAccount: !Ref AWS::AccountId" in template
    assert "aws:SourceArn: !GetAtt DatabaseCredentialRefreshScheduleGroup.Arn" in template
    assert "Type: AWS::Scheduler::ScheduleGroup" in template
    assert "GroupName: !Ref DatabaseCredentialRefreshScheduleGroup" in template
    assert "RoleArn: !GetAtt ScheduledRefreshRole.Arn" in template
    assert "ApplyOnlyAtCronInterval" not in template
    assert "Type: AWS::Events::Rule" in template
    assert "EC2 Command Status-change Notification" in template
    assert "document-name:" in template
    assert "- Failed" in template
    assert "- TimedOut" in template
    assert "- Cancelled" in template
    assert "Type: AWS::SNS::Topic" in template
    assert "KmsMasterKeyId: alias/aws/sns" in template
    assert "Type: AWS::DynamoDB::Table" in template
    assert "attribute_not_exists(alert_key) OR expires_at < :now" in template
    assert 'f"test:{nonce}" if is_test else "production"' in template
    assert '"instanceId": instance_id' in template
    assert '"documentName": document_name' in template
    assert '"status": status' in template
    notifier = _notifier_source()
    assert "StandardOutputContent" not in notifier
    assert "StandardErrorContent" not in notifier
    assert "command-id" not in notifier
    assert "password" not in notifier.lower()
    test_publish_policy = template.split(
        "- Sid: PublishDisposableRefreshAlertTestEvent", 1
    )[1].split("- Sid: ObserveDisposableRefreshAlertTest", 1)[0]
    assert "events:source: university-portal.database-refresh-alert-test" in (
        test_publish_policy
    )
    assert "events:detail-type: Database Refresh Alert Test" in test_publish_policy
    assert "source: aws.ssm" not in test_publish_policy
    compile(notifier, "<database-refresh-alert>", "exec")

    literal = template.split("                - !Sub |\n", 1)[1]
    literal = literal.split("\n\n  ScheduledRefreshRole:", 1)[0]
    script = textwrap.dedent(literal)
    syntax = subprocess.run(
        ["bash", "-n"], input=script, text=True, capture_output=True, check=False
    )
    assert syntax.returncode == 0, syntax.stderr
    heredocs = re.findall(r"<<'([A-Z_]+)'\n(.*?)\n\s*\1", script, re.DOTALL)
    assert {name for name, _ in heredocs} == {
        "PY_VERIFY",
        "PY_IDLE",
        "PY_BOOTSTRAP",
        "PY_CA",
        "PY_CA_COMBINED",
        "PY_CONFIG",
        "PY_SMOKE",
    }
    for name, source in heredocs:
        compile(textwrap.dedent(source), f"<database-refresh-{name}>", "exec")


class _NotifierDynamoDB:
    def __init__(
        self,
        *,
        duplicate: bool = False,
        fail_update: bool = False,
    ) -> None:
        self.duplicate = duplicate
        self.fail_update = fail_update
        self.put_calls: list[dict[str, object]] = []
        self.update_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []

    def put_item(self, **kwargs):
        self.put_calls.append(kwargs)
        if self.duplicate:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}},
                "PutItem",
            )

    def update_item(self, **kwargs):
        self.update_calls.append(kwargs)
        if self.fail_update:
            raise RuntimeError("simulated marker failure")

    def delete_item(self, **kwargs):
        self.delete_calls.append(kwargs)


class _NotifierSNS:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.publish_calls: list[dict[str, object]] = []

    def publish(self, **kwargs):
        self.publish_calls.append(kwargs)
        if self.fail:
            raise RuntimeError("simulated SNS failure")


def _load_notifier(
    ddb: _NotifierDynamoDB,
    sns: _NotifierSNS,
) -> dict[str, object]:
    fake_boto3 = types.SimpleNamespace(
        client=lambda name: {"dynamodb": ddb, "sns": sns}[name]
    )
    namespace: dict[str, object] = {}
    environment = {
        "ALERT_TOPIC_ARN": "arn:aws:sns:ap-south-1:123456789012:test",
        "DEDUPLICATION_TABLE": "test-alert-deduplication",
        "DEDUPLICATION_MINUTES": "60",
        "EXPECTED_DOCUMENT": "university-portal-database-credential-refresh",
        "EXPECTED_INSTANCE": "i-production",
    }
    with (
        patch.dict(sys.modules, {"boto3": fake_boto3}),
        patch.dict(os.environ, environment, clear=False),
    ):
        exec(compile(
            _notifier_source(),
            "<database-refresh-alert>",
            "exec",
        ), namespace)
    return namespace


def _production_failure_event() -> dict[str, object]:
    return {
        "source": "aws.ssm",
        "detail-type": "EC2 Command Status-change Notification",
        "detail": {
            "instance-id": "i-production",
            "document-name": "university-portal-database-credential-refresh",
            "status": "Failed",
            "command-id": "must-not-be-published",
        },
    }


def test_notifier_publishes_only_sanitized_failure_identity() -> None:
    ddb = _NotifierDynamoDB()
    sns = _NotifierSNS()
    handler = _load_notifier(ddb, sns)["handler"]

    assert handler(_production_failure_event(), None) == {"outcome": "published"}
    assert len(sns.publish_calls) == 1
    assert json.loads(sns.publish_calls[0]["Message"]) == {
        "documentName": "university-portal-database-credential-refresh",
        "instanceId": "i-production",
        "status": "Failed",
    }
    assert len(ddb.put_calls) == 1
    assert ddb.update_calls == []
    assert ddb.delete_calls == []


def test_notifier_rejects_non_ssm_source_and_suppresses_duplicate() -> None:
    invalid_ddb = _NotifierDynamoDB()
    invalid_sns = _NotifierSNS()
    invalid_handler = _load_notifier(invalid_ddb, invalid_sns)["handler"]
    invalid_event = _production_failure_event()
    invalid_event["source"] = "user.forged"
    assert invalid_handler(invalid_event, None) == {"outcome": "ignored"}
    assert invalid_ddb.put_calls == []
    assert invalid_sns.publish_calls == []

    duplicate_ddb = _NotifierDynamoDB(duplicate=True)
    duplicate_sns = _NotifierSNS()
    duplicate_handler = _load_notifier(duplicate_ddb, duplicate_sns)["handler"]
    assert duplicate_handler(_production_failure_event(), None) == {
        "outcome": "suppressed"
    }
    assert duplicate_sns.publish_calls == []


def test_notifier_removes_deduplication_record_when_publish_fails() -> None:
    ddb = _NotifierDynamoDB()
    sns = _NotifierSNS(fail=True)
    handler = _load_notifier(ddb, sns)["handler"]

    with pytest.raises(RuntimeError, match="simulated SNS failure"):
        handler(_production_failure_event(), None)
    assert len(ddb.delete_calls) == 1


def test_notifier_keeps_lock_when_test_marker_update_fails_after_publish() -> None:
    ddb = _NotifierDynamoDB(fail_update=True)
    sns = _NotifierSNS()
    handler = _load_notifier(ddb, sns)["handler"]
    event = {
        "source": "university-portal.database-refresh-alert-test",
        "detail-type": "Database Refresh Alert Test",
        "detail": {
            "instance-id": "i-production",
            "document-name": "university-portal-database-credential-refresh",
            "status": "Failed",
            "test-nonce": "a" * 32,
        },
    }

    with pytest.raises(RuntimeError, match="simulated marker failure"):
        handler(event, None)
    assert len(sns.publish_calls) == 1
    assert len(ddb.update_calls) == 1
    assert ddb.delete_calls == []


class _Events:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def put_events(self, **kwargs):
        self.calls.append(kwargs)
        return {"FailedEntryCount": 0}


class _DynamoDB:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def get_item(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "Item": {
                "published_at": {"N": "1"},
                "suppressed_count": {"N": "1"},
            }
        }


class _AlertSession:
    def __init__(self, events: _Events, ddb: _DynamoDB) -> None:
        self.events = events
        self.ddb = ddb

    def client(self, name: str, region_name: str):
        assert region_name == "ap-south-1"
        return {"events": self.events, "dynamodb": self.ddb}[name]


def test_disposable_alert_proof_publishes_once_and_suppresses_repeat(capsys) -> None:
    events = _Events()
    ddb = _DynamoDB()
    with (
        patch.object(
            alert_client, "_session", return_value=_AlertSession(events, ddb)
        ),
        patch.object(alert_client.uuid, "uuid4", return_value=type(
            "_Uuid", (), {"hex": "a" * 32}
        )()),
    ):
        alert_client.prove("i-production", "ap-south-1")

    assert len(events.calls) == 1
    entries = events.calls[0]["Entries"]
    assert len(entries) == 2
    assert entries[0] == entries[1]
    assert entries[0]["Source"] == alert_client.TEST_SOURCE
    assert entries[0]["DetailType"] == alert_client.TEST_DETAIL_TYPE
    assert json.loads(entries[0]["Detail"]) == {
        "instance-id": "i-production",
        "document-name": "university-portal-database-credential-refresh",
        "status": "Failed",
        "test-nonce": "a" * 32,
    }
    assert ddb.calls == [{
        "TableName": alert_client.DEFAULT_TABLE,
        "Key": {
            "alert_key": {
                "S": (
                    "i-production|university-portal-database-credential-refresh|"
                    f"test:{'a' * 32}"
                )
            }
        },
        "ConsistentRead": True,
    }]
    assert capsys.readouterr().out.strip() == (
        "database-refresh-alert-published-and-repeat-suppressed"
    )


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