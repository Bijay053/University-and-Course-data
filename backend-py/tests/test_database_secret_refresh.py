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

DEPLOY_SPEC = importlib.util.spec_from_file_location(
    "database_refresh_deploy",
    DEPLOY_DIR / "deploy_database_secret_rotation_stack.py",
)
assert DEPLOY_SPEC and DEPLOY_SPEC.loader
deploy_client = importlib.util.module_from_spec(DEPLOY_SPEC)
DEPLOY_SPEC.loader.exec_module(deploy_client)

ALERT_SPEC = importlib.util.spec_from_file_location(
    "database_refresh_alert",
    DEPLOY_DIR / "prove_database_refresh_alert.py",
)
assert ALERT_SPEC and ALERT_SPEC.loader
alert_client = importlib.util.module_from_spec(ALERT_SPEC)
ALERT_SPEC.loader.exec_module(alert_client)

DELIVERY_PROOF_SPEC = importlib.util.spec_from_file_location(
    "database_refresh_alert_delivery",
    DEPLOY_DIR / "prove_database_refresh_alert_delivery.py",
)
assert DELIVERY_PROOF_SPEC and DELIVERY_PROOF_SPEC.loader
delivery_proof = importlib.util.module_from_spec(DELIVERY_PROOF_SPEC)
DELIVERY_PROOF_SPEC.loader.exec_module(delivery_proof)

REHEARSAL_SPEC = importlib.util.spec_from_file_location(
    "database_refresh_rehearsal",
    DEPLOY_DIR / "rehearse_database_secret_refresh.py",
)
assert REHEARSAL_SPEC and REHEARSAL_SPEC.loader
rehearsal = importlib.util.module_from_spec(REHEARSAL_SPEC)
REHEARSAL_SPEC.loader.exec_module(rehearsal)


def test_disposable_rehearsal_is_explicitly_guarded_and_uses_isolated_fixture() -> None:
    fixture = (DEPLOY_DIR / "database-secret-refresh-rehearsal.yaml").read_text()
    source = (DEPLOY_DIR / "rehearse_database_secret_refresh.py").read_text()
    readme = (DEPLOY_DIR / "README.md").read_text()
    assert "ManageMasterUserPassword: true" in fixture
    assert "DBInstanceClass: db.t3.micro" in fixture
    assert "DBInstanceClass: db.t4g.micro" not in fixture
    assert "PubliclyAccessible: false" in fixture
    assert "Type: AWS::Scheduler::ScheduleGroup" in fixture
    assert "ScheduleExpression: rate(5 minutes)" in fixture
    assert "State: DISABLED" in fixture
    assert "arn:aws:scheduler:::aws-sdk:ssm:sendCommand" in fixture
    assert 'sslmode="verify-full"' in fixture
    assert 'connection.execute("SELECT 1")' in fixture
    assert "university-portal:disposable-rehearsal" in fixture
    assert "scheduler-origin disposable database refresh rehearsal" in fixture
    assert "RotateMasterUserPassword=True" in source
    assert "VersionIdsToStages" in source
    assert '@celery_app.task(name="scrape.university")' in fixture
    assert "executions.sqlite3" in fixture
    assert "redis6-cli llen scrape" in fixture
    assert "redis-cli llen scrape" not in fixture
    assert fixture.count("cancel_consumer scrape --timeout=20") == 2
    assert fixture.count("control.inspect(timeout=20).active()") == 2
    assert fixture.count("add_consumer scrape --timeout=20") == 1
    assert (
        'aws:SourceArn: !Sub "arn:${AWS::Partition}:scheduler:${AWS::Region}:'
        '${AWS::AccountId}:schedule-group/up-db-refresh-${RehearsalId}"'
    ) in fixture
    assert "905043442097" in source
    assert "PaginationToken" in source
    assert "Type: AWS::SQS::Queue" not in fixture
    assert "redis://127.0.0.1:6379/0" in fixture
    assert "cloud-init status --wait >/dev/null" in fixture
    assert "touch /var/lib/up-rehearsal/bootstrap.complete" in fixture
    assert "inspect ping --timeout=5" in fixture
    assert fixture.count(
        "until test -f /var/lib/up-rehearsal/bootstrap.complete"
    ) == 2
    assert (
        "/opt/up-rehearsal/.venv/bin/python - "
        "'${Database.MasterUserSecret.SecretArn}'" in fixture
    )
    assert "deadline=time.monotonic()+120" in fixture
    assert 'State="ENABLED"' in source
    assert "describe_instance_information" in source
    assert "NatGatewayId" in source
    assert "RDS managed secret remains after teardown" in source
    assert source.index("scheduler.update_schedule(") < source.index(
        "cf.delete_stack("
    )
    assert "--i-understand-this-creates-disposable-aws-resources" in source
    assert "--fail-at" in source
    assert "--state-output" in source
    assert "get_caller_identity" in source
    assert "describe_secret" in source
    assert ".get_secret_value(" in source
    assert "print(value)" not in source
    assert "DISPOSABLE_AWS_ACCESS_KEY_ID" in source
    assert "DISPOSABLE_AWS_SECRET_ACCESS_KEY" in source
    assert "AWS_SSM_ACCESS_KEY_ID" not in source
    for required_argument in (
        "--proof-output",
        "--proof-signing-key-id",
        "--expected-account-id",
        "--production-account-id",
        "--vpc-id",
        "--private-subnet-id",
        "--second-private-subnet-id",
        "--i-understand-this-creates-disposable-aws-resources",
    ):
        assert required_argument in source
        assert required_argument in readme


def test_rehearsal_runner_logical_resource_lookups_exist_in_fixture() -> None:
    fixture = (DEPLOY_DIR / "database-secret-refresh-rehearsal.yaml").read_text()
    source = (DEPLOY_DIR / "rehearse_database_secret_refresh.py").read_text()
    logical_ids = set(
        re.findall(r"^  ([A-Za-z][A-Za-z0-9]+):\n    Type: AWS::", fixture, re.MULTILINE)
    )
    lookups = set(re.findall(r'tagged\["([^"]+)"\]', source))
    assert lookups
    assert lookups <= logical_ids


def _rehearsal_execution_wait_source() -> str:
    fixture = (DEPLOY_DIR / "database-secret-refresh-rehearsal.yaml").read_text()
    marker = ".venv/bin/python - '${RehearsalId}' <<'PY'\n"
    assert fixture.count(marker) == 1
    source = fixture.split(marker, 1)[1].split("\n                    PY", 1)[0]
    return textwrap.dedent(source)


@pytest.mark.parametrize(
    ("execution_count", "expected_returncode", "expected_error"),
    [
        (None, 1, "queued probe did not execute"),
        (1, 0, ""),
        (2, 1, "queued probe executed more than once"),
    ],
)
def test_rehearsal_execution_wait_runs_against_sqlite(
    tmp_path: Path,
    execution_count: int | None,
    expected_returncode: int,
    expected_error: str,
) -> None:
    """Execute the template's real exactly-once check, not a rewritten analogue."""
    run_id = "0123456789abcdef"
    database = tmp_path / "executions.sqlite3"
    if execution_count is not None:
        import sqlite3

        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TABLE executions(run_id text primary key,count integer not null)"
            )
            connection.execute(
                "INSERT INTO executions(run_id,count) VALUES(?,?)",
                (run_id, execution_count),
            )

    source = _rehearsal_execution_wait_source().replace(
        '"/var/lib/up-rehearsal/executions.sqlite3"',
        repr(str(database)),
    )
    # Keep failure coverage fast while preserving the extracted control flow.
    source = source.replace(
        "deadline = time.monotonic() + 60",
        "deadline = time.monotonic() + 0.01",
    )
    result = subprocess.run(
        [sys.executable, "-", run_id],
        input=source,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == expected_returncode
    assert expected_error in result.stderr


def test_rehearsal_embedded_shell_and_python_are_syntactically_executable() -> None:
    fixture = (DEPLOY_DIR / "database-secret-refresh-rehearsal.yaml").read_text()
    shell_blocks = re.findall(
        r"                - !Sub \|\n(.*?)(?=\n  [A-Za-z]|\nOutputs:)",
        fixture,
        re.DOTALL,
    )
    assert len(shell_blocks) == 2
    for index, block in enumerate(shell_blocks):
        script = textwrap.dedent(block)
        syntax = subprocess.run(
            ["bash", "-n"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        assert syntax.returncode == 0, f"shell block {index}: {syntax.stderr}"
        heredocs = re.findall(r"<<'PY'\n(.*?)\n\s*PY", script, re.DOTALL)
        assert heredocs
        for heredoc_index, source in enumerate(heredocs):
            compile(
                textwrap.dedent(source),
                f"<rehearsal-{index}-{heredoc_index}>",
                "exec",
            )


def test_rehearsal_refuses_without_opt_in_before_any_aws_call() -> None:
    with pytest.raises(RuntimeError, match="i-understand"):
        rehearsal.rehearse(
            expected_account="123456789012",
            production_account="210987654321",
            region="ap-south-1",
            vpc_id="vpc-test",
            private_subnet_id="subnet-a",
            second_private_subnet_id="subnet-b",
            stack_name="test",
            opt_in=False,
        )


def test_rehearsal_failure_injection_is_bounded_to_named_checkpoints() -> None:
    for checkpoint in rehearsal.FAILURE_CHECKPOINTS:
        with pytest.raises(
            RuntimeError, match=f"injected rehearsal failure at {checkpoint}"
        ):
            rehearsal._inject_failure(checkpoint, checkpoint)
        rehearsal._inject_failure(None, checkpoint)
        rehearsal._inject_failure("different-checkpoint", checkpoint)


def test_rehearsal_cleanup_failures_remain_visible_with_injected_failure() -> None:
    primary = RuntimeError("injected rehearsal failure at after-stack-creation")
    rehearsal._report_cleanup_errors(
        ["disable scheduler failed: denied", "stack deletion failed: timeout"],
        primary,
    )
    assert primary.__notes__ == [
        "rehearsal cleanup failed: disable scheduler failed: denied; "
        "stack deletion failed: timeout"
    ]


def test_rehearsal_cleanup_failure_is_raised_without_primary_failure() -> None:
    with pytest.raises(RuntimeError, match="stack deletion failed: timeout"):
        rehearsal._report_cleanup_errors(
            ["stack deletion failed: timeout"],
            None,
        )


class _ResidueEc2:
    def describe_instances(self, *, InstanceIds):
        assert InstanceIds == ["i-0123456789abcdef0", "i-0fedcba9876543210"]
        return {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-0fedcba9876543210",
                            "State": {"Name": "terminated"},
                        },
                        {
                            "InstanceId": "i-0123456789abcdef0",
                            "State": {"Name": "running"},
                        },
                    ]
                }
            ]
        }


def test_rehearsal_ignores_only_proven_terminated_ec2_tag_index_ghosts() -> None:
    residues = [
        {
            "ResourceARN": (
                "arn:aws:ec2:ap-south-1:123456789012:"
                "instance/i-0fedcba9876543210"
            )
        },
        {
            "ResourceARN": (
                "arn:aws:ec2:ap-south-1:123456789012:"
                "instance/i-0123456789abcdef0"
            )
        },
        {
            "ResourceARN": (
                "arn:aws:rds:ap-south-1:123456789012:db:still-present"
            )
        },
    ]
    assert rehearsal._exclude_terminated_instance_residues(
        _ResidueEc2(), residues
    ) == residues[1:]


def test_rehearsal_never_falls_back_to_production_aws_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DISPOSABLE_AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("DISPOSABLE_AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setenv("AWS_SSM_ACCESS_KEY_ID", "production-must-not-be-used")
    monkeypatch.setenv("AWS_SSM_SECRET_ACCESS_KEY", "production-must-not-be-used")
    with pytest.raises(RuntimeError, match="dedicated DISPOSABLE"):
        rehearsal._session()


class _RehearsalSts:
    def get_caller_identity(self):
        return {"Account": "123456789012"}


class _RehearsalSession:
    def client(self, name, region_name):
        assert name == "sts"
        return _RehearsalSts()


def test_rehearsal_requires_the_exact_run_tag_not_merely_tag_presence() -> None:
    session = _RehearsalSession()
    with pytest.raises(RuntimeError, match="this run"):
        rehearsal._require_disposable(
            session, "ap-south-1", "123456789012", "run-a",
            [{"Key": rehearsal.TAG_KEY, "Value": "run-b"}],
        )
    rehearsal._require_disposable(
        session, "ap-south-1", "123456789012", "run-a",
        [{"Key": rehearsal.TAG_KEY, "Value": "run-a"}],
    )


class _SafeSecret:
    def describe_secret(self, **_kwargs):
        return {
            "ARN": "arn:aws:secretsmanager:ap-south-1:123456789012:secret:fixture",
            "OwningService": "rds",
            "VersionIdsToStages": {"version": ["AWSCURRENT"]},
        }

    def get_secret_value(self, **_kwargs):
        return {"SecretString": json.dumps({"username": "u", "password": "p"})}


def test_rehearsal_accepts_managed_secret_without_endpoint_fields() -> None:
    assert rehearsal._managed_secret_shape(_SafeSecret(), "fixture")["ARN"]


def test_known_production_account_is_immutably_rejected() -> None:
    with pytest.raises(RuntimeError, match="production"):
        rehearsal._require_account(
            _RehearsalSession(), "ap-south-1", "905043442097", "000000000000"
        )


def _notifier_source() -> str:
    template = (DEPLOY_DIR / "database-secret-rotation-iam.yaml").read_text()
    source = template.split("ZipFile: |\n", 1)[1].split(
        "\n\n  DatabaseRefreshAlertDeadLetterQueue:", 1
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

    def get_document(self, **kwargs):
        return {
            "DocumentVersion": kwargs.get("DocumentVersion", "7").replace("$DEFAULT", "7"),
            "Content": '{"description":"template-revision=revision-a"}',
        }

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
        "DocumentVersion": "7",
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

class _Waiter:
    def wait(self, **_kwargs):
        return None
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
    assert "cloudformation:" not in deployment_policy
    assert "iam:PassRole" not in deployment_policy
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
    assert template.count("timeout 150s systemctl restart uni-api-py uni-celery") == 1
    assert "control cancel_consumer scrape" in template
    assert "celery_app.control.inspect(timeout=10).active()" in template
    assert 'task.get("name") == "scrape.university"' in template
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
    assert "Type: AWS::KMS::Key" in template
    assert "EnableKeyRotation: true" in template
    assert "KmsMasterKeyId: !Ref DatabaseRefreshAlertEncryptionKey" in template
    assert "alias/aws/sns" not in template
    assert "alias/aws/sqs" not in template
    assert "Service: events.amazonaws.com" in template
    assert "Service: cloudwatch.amazonaws.com" in template
    assert (
        "rule/university-portal-database-refresh-*"
    ) in template
    assert (
        "alarm:university-portal-database-refresh-*"
    ) in template
    assert "Action:\n                  - kms:Decrypt\n                  - kms:GenerateDataKey" in template
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
    assert "Type: AWS::SQS::Queue" in template
    assert "MessageRetentionPeriod: 1209600" in template
    assert "Type: AWS::Lambda::EventInvokeConfig" in template
    assert "MaximumRetryAttempts: 2" in template
    assert "Destination: !GetAtt DatabaseRefreshAlertDeadLetterQueue.Arn" in template
    assert template.count(
        "DeadLetterConfig:\n            Arn: !GetAtt DatabaseRefreshAlertDeadLetterQueue.Arn"
    ) == 2
    assert template.count("InputTransformer:") == 2
    assert "<instanceId>" in template
    assert "<documentName>" in template
    assert "<status>" in template
    assert "command-id" not in template
    assert "StandardOutputContent" not in template
    assert "StandardErrorContent" not in template
    assert "Type: AWS::CloudWatch::Alarm" in template
    assert "MetricName: ApproximateNumberOfMessagesVisible" in template
    assert "MetricName: Errors" in template
    assert (
        "AlarmDescription: Database refresh alert delivery exhausted retries; "
        "inspect the encrypted dead-letter queue"
    ) in template
    delivery_observe_policy = template.split(
        "- Sid: ObserveRefreshAlertDeliveryAlarm", 1
    )[1].split("- Sid: ProveRefreshAlertDeliveryAlarm", 1)[0]
    assert "Action: cloudwatch:DescribeAlarms" in delivery_observe_policy
    assert 'Resource: "*"' in delivery_observe_policy
    delivery_proof_policy = template.split(
        "- Sid: ProveRefreshAlertDeliveryAlarm", 1
    )[1].split("ProductionDatabaseSecretReadPolicy:", 1)[0]
    assert "Action: cloudwatch:SetAlarmState" in delivery_proof_policy
    assert "cloudwatch:DescribeAlarms" not in delivery_proof_policy
    assert (
        "alarm:university-portal-database-refresh-alert-delivery-failed"
    ) in delivery_proof_policy
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


class _CloudWatch:
    def __init__(self, initial: str = "OK") -> None:
        self.state = initial
        self.set_calls: list[dict[str, str]] = []

    def describe_alarms(self, **_kwargs):
        return {
            "MetricAlarms": [{
                "AlarmName": delivery_proof.ALARM_NAME,
                "StateValue": self.state,
            }]
        }

    def set_alarm_state(self, **kwargs):
        self.set_calls.append(kwargs)
        self.state = kwargs["StateValue"]


class _CloudWatchSession:
    def __init__(self, cloudwatch: _CloudWatch) -> None:
        self.cloudwatch = cloudwatch

    def client(self, name: str, region_name: str):
        assert name == "cloudwatch"
        assert region_name == "ap-south-1"
        return self.cloudwatch


def test_disposable_delivery_alarm_proof_restores_prior_state(capsys) -> None:
    cloudwatch = _CloudWatch(initial="INSUFFICIENT_DATA")
    with patch.object(
        delivery_proof,
        "_session",
        return_value=_CloudWatchSession(cloudwatch),
    ):
        delivery_proof.prove("ap-south-1")

    assert [call["StateValue"] for call in cloudwatch.set_calls] == [
        "ALARM",
        "INSUFFICIENT_DATA",
    ]
    assert cloudwatch.set_calls[0]["StateReason"] == delivery_proof.TEST_REASON
    assert cloudwatch.set_calls[1]["StateReason"] == delivery_proof.RESTORE_REASON
    assert capsys.readouterr().out.strip() == (
        "database-refresh-alert-delivery-alarm-proved"
    )


def test_delivery_alarm_proof_refuses_to_override_real_alarm() -> None:
    cloudwatch = _CloudWatch(initial="ALARM")
    with patch.object(
        delivery_proof,
        "_session",
        return_value=_CloudWatchSession(cloudwatch),
    ):
        with pytest.raises(RuntimeError, match="investigate instead of testing"):
            delivery_proof.prove("ap-south-1")
    assert cloudwatch.set_calls == []


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

class _DeploySSM:
    def __init__(
        self,
        cloudformation: _CloudFormation,
        region: str = "ap-south-1",
        secret_arn: str = (
            "arn:aws:secretsmanager:ap-south-1:123456789012:secret:test"
        ),
        template_body: str | None = None,
    ) -> None:
        self.cloudformation = cloudformation
        self.region = region
        self.secret_arn = secret_arn
        self.template_body = template_body or (
            DEPLOY_DIR / "database-secret-rotation-iam.yaml"
        ).read_text()

    def get_document(self, **_kwargs):
        content_hash = deploy_client._document_content_hash(
            self.template_body,
            self.region,
            self.secret_arn,
        )
        return {
            "DocumentVersion": "9",
            "Content": json.dumps(deploy_client._expected_document_content(
                self.template_body,
                self.region,
                self.secret_arn,
                self.cloudformation.revision,
                content_hash,
            )),
        }

    def describe_document(self, **_kwargs):
        return {"Document": {"DefaultVersion": "9"}}

class _DeploySession:
    def __init__(self, cloudformation: _CloudFormation) -> None:
        self.cloudformation = cloudformation
        self.ssm = _DeploySSM(cloudformation)
        self.ddb = _DeployDynamoDB()

    def client(self, name: str, region_name: str):
        assert region_name == "ap-south-1"
        return {
            "cloudformation": self.cloudformation,
            "ssm": self.ssm,
            "dynamodb": self.ddb,
        }[name]

class _CloudFormation:
    def __init__(self, revision: str | None) -> None:
        self.revision = revision
        self.status = "UPDATE_COMPLETE"
        self.document_name = deploy_client.DOCUMENT_NAME
        self.update_calls: list[dict[str, object]] = []

    def describe_stacks(self, **_kwargs):
        outputs = [{
            "OutputKey": deploy_client.DOCUMENT_OUTPUT,
            "OutputValue": self.document_name,
        }]
        if self.revision is not None:
            outputs.insert(0, {
                "OutputKey": deploy_client.REVISION_OUTPUT,
                "OutputValue": self.revision,
            })
        return {
            "Stacks": [{
                "StackStatus": self.status,
                "Outputs": outputs,
            }]
        }

    def update_stack(self, **kwargs):
        self.update_calls.append(kwargs)
        self.revision = next(
            item["ParameterValue"]
            for item in kwargs["Parameters"]
            if item["ParameterKey"] == "TemplateRevision"
        )

    def get_waiter(self, name: str):
        assert name == "stack_update_complete"
        return _Waiter()

class _DeployDynamoDB:
    def __init__(self) -> None:
        self.owner: str | None = None

    def put_item(self, **kwargs):
        if self.owner is not None:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}},
                "PutItem",
            )
        self.owner = kwargs["Item"]["owner"]["S"]

    def delete_item(self, **kwargs):
        if kwargs["ExpressionAttributeValues"][":owner"]["S"] != self.owner:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}},
                "DeleteItem",
            )
        self.owner = None

    def update_item(self, **kwargs):
        if kwargs["ExpressionAttributeValues"][":owner"]["S"] != self.owner:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}},
                "UpdateItem",
            )

def test_two_repository_revisions_cannot_silently_overwrite_each_other() -> None:
    cloudformation = _CloudFormation("revision-old")
    session = _DeploySession(cloudformation)
    common = {
        "stack_name": deploy_client.STACK_NAME,
        "instance_id": "i-production",
        "secret_arn": "arn:aws:secretsmanager:ap-south-1:123456789012:secret:test",
        "alert_email": "operator@example.test",
        "region": "ap-south-1",
        "expected_revision": "revision-old",
        "template_body": (
            DEPLOY_DIR / "database-secret-rotation-iam.yaml"
        ).read_text(),
    }
    with patch.object(deploy_client, "_session", return_value=session):
        deploy_client.deploy(revision="revision-newer", **common)
        with pytest.raises(RuntimeError, match="Stale deployment rejected"):
            deploy_client.deploy(revision="revision-stale", **common)

    assert cloudformation.revision == "revision-newer"
    assert len(cloudformation.update_calls) == 1
    parameters = cloudformation.update_calls[0]["Parameters"]
    preserved = {
        item["ParameterKey"]
        for item in parameters
        if item.get("UsePreviousValue") is True
    }
    assert preserved == {
        "DeploymentUserName",
        "ProductionInstanceRoleName",
        "ScheduleState",
        "AlertDeduplicationMinutes",
    }

def test_competing_deployment_cannot_pass_the_conditional_lock() -> None:
    cloudformation = _CloudFormation("revision-old")
    session = _DeploySession(cloudformation)
    session.ddb.owner = "other-deployment"
    with (
        patch.object(deploy_client, "_session", return_value=session),
        pytest.raises(RuntimeError, match="holds the lock"),
    ):
        deploy_client.deploy(
            stack_name=deploy_client.STACK_NAME,
            instance_id="i-production",
            secret_arn=(
                "arn:aws:secretsmanager:ap-south-1:123456789012:secret:test"
            ),
            alert_email="operator@example.test",
            region="ap-south-1",
            revision="revision-other",
            expected_revision="revision-old",
            template_body=(
                DEPLOY_DIR / "database-secret-rotation-iam.yaml"
            ).read_text(),
        )
    assert cloudformation.update_calls == []

def test_unversioned_stack_can_be_adopted_once_then_requires_fencing() -> None:
    cloudformation = _CloudFormation(None)
    session = _DeploySession(cloudformation)
    common = {
        "stack_name": deploy_client.STACK_NAME,
        "instance_id": "i-production",
        "secret_arn": "arn:aws:secretsmanager:ap-south-1:123456789012:secret:test",
        "alert_email": "operator@example.test",
        "region": "ap-south-1",
        "expected_revision": None,
        "template_body": (
            DEPLOY_DIR / "database-secret-rotation-iam.yaml"
        ).read_text(),
    }
    with patch.object(deploy_client, "_session", return_value=session):
        with pytest.raises(RuntimeError, match="has no revision fence"):
            deploy_client.deploy(revision="revision-adopted", **common)
        deploy_client.deploy(
            revision="revision-adopted",
            adopt_unversioned_stack=True,
            **common,
        )
        with pytest.raises(RuntimeError, match="only valid before"):
            deploy_client.deploy(
                revision="revision-bypass",
                adopt_unversioned_stack=True,
                **common,
            )

    assert cloudformation.revision == "revision-adopted"
    assert len(cloudformation.update_calls) == 1

def test_changed_template_cannot_reuse_the_checked_out_revision() -> None:
    with (
        patch.object(
            deploy_client.subprocess,
            "check_output",
            side_effect=[
                "",
                "revision-old\n",
                "committed template content\n",
            ],
        ),
        patch.object(
            Path,
            "read_text",
            return_value="changed template content\n",
        ),
        pytest.raises(RuntimeError, match="does not match the checked-out revision"),
    ):
        deploy_client._verified_repository_source()

def test_dirty_repository_cannot_be_deployed() -> None:
    with (
        patch.object(
            deploy_client.subprocess,
            "check_output",
            return_value=" M backend-py/deploy/database-secret-rotation-iam.yaml\n",
        ),
        pytest.raises(RuntimeError, match="requires a clean"),
    ):
        deploy_client._verified_repository_source()

def test_deploy_reuses_verified_template_without_rereading_disk() -> None:
    template_body = (
        DEPLOY_DIR / "database-secret-rotation-iam.yaml"
    ).read_text()
    cloudformation = _CloudFormation("revision-old")
    session = _DeploySession(cloudformation)
    with (
        patch.object(deploy_client, "_session", return_value=session),
        patch.object(
            Path,
            "read_text",
            side_effect=AssertionError("mutable template path was reread"),
        ),
    ):
        deploy_client.deploy(
            stack_name=deploy_client.STACK_NAME,
            instance_id="i-production",
            secret_arn=(
                "arn:aws:secretsmanager:ap-south-1:123456789012:secret:test"
            ),
            alert_email="operator@example.test",
            region="ap-south-1",
            revision="revision-new",
            template_body=template_body,
            expected_revision="revision-old",
        )

    assert cloudformation.update_calls[0]["TemplateBody"] == template_body
