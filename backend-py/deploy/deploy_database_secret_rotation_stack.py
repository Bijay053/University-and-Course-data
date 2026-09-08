#!/usr/bin/env python3
"""Deploy the database rotation stack with stale-revision fencing."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import textwrap
import time
import uuid

from botocore.exceptions import ClientError
import yaml

from install_openai_fallback_via_ssm import _session


DOCUMENT_NAME = "university-portal-database-credential-refresh"
STACK_NAME = "university-portal-database-secret-rotation"
REVISION_OUTPUT = "DeployedTemplateRevision"
DOCUMENT_OUTPUT = "DatabaseCredentialRefreshDocumentName"
TEMPLATE = Path(__file__).with_name("database-secret-rotation-iam.yaml")
LOCK_TABLE = "university-portal-database-refresh-alert-deduplication"
LOCK_KEY = "deployment-lock"
LOCK_SECONDS = 14400


def _outputs(stack: dict[str, object]) -> dict[str, str]:
    return {
        item["OutputKey"]: item["OutputValue"]
        for item in stack.get("Outputs", [])
    }


def _verified_repository_source() -> tuple[str, str]:
    status = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=TEMPLATE.parents[2],
        text=True,
    )
    if status.strip():
        raise RuntimeError("Deployment requires a clean checked-out repository")
    revision = subprocess.check_output(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=TEMPLATE.parents[2],
        text=True,
    ).strip()
    committed_template = subprocess.check_output(
        [
            "git",
            "show",
            f"{revision}:backend-py/deploy/{TEMPLATE.name}",
        ],
        cwd=TEMPLATE.parents[2],
        text=True,
    )
    if committed_template != TEMPLATE.read_text(encoding="utf-8"):
        raise RuntimeError("Deployment template does not match the checked-out revision")
    return revision, committed_template


def _describe_stack(cloudformation, stack_name: str) -> dict[str, object] | None:
    try:
        response = cloudformation.describe_stacks(StackName=stack_name)
    except ClientError as exc:
        if "does not exist" in str(exc):
            return None
        raise
    return response["Stacks"][0]


class _TemplateLoader(yaml.SafeLoader):
    pass


_TemplateLoader.add_constructor(
    "!Sub",
    lambda loader, node: loader.construct_scalar(node),
)


def _replace(value, replacements: dict[str, str]):
    if isinstance(value, str):
        for old, new in replacements.items():
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [_replace(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace(item, replacements) for key, item in value.items()}
    return value


def _expected_document_content(
    template_body: str,
    region: str,
    secret_arn: str,
    revision: str,
    content_hash: str,
) -> dict[str, object]:
    source = template_body.split("      Content:\n", 1)[1].split(
        "\n\n  ScheduledRefreshRole:", 1
    )[0]
    document = yaml.load(textwrap.dedent(source), Loader=_TemplateLoader)
    return _replace(document, {
        "${AWS::Region}": region,
        "${DatabaseSecretArn}": secret_arn,
        "${TemplateRevision}": revision,
        "${DocumentContentHash}": content_hash,
    })


def _document_content_hash(
    template_body: str,
    region: str,
    secret_arn: str,
) -> str:
    content = _expected_document_content(
        template_body,
        region,
        secret_arn,
        "REVISION",
        "HASH",
    )
    canonical_steps = json.dumps(
        content["mainSteps"],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_steps.encode()).hexdigest()


def _verify_document(
    ssm,
    *,
    template_body: str,
    region: str,
    secret_arn: str,
    revision: str,
    content_hash: str,
) -> str:
    response = ssm.get_document(Name=DOCUMENT_NAME, DocumentVersion="$DEFAULT")
    content = json.loads(response["Content"])
    expected = _expected_document_content(
        template_body,
        region,
        secret_arn,
        revision,
        content_hash,
    )
    if content != expected:
        raise RuntimeError(
            "SSM document content does not match the deployed template revision"
        )
    version = response["DocumentVersion"]
    metadata = ssm.describe_document(Name=DOCUMENT_NAME)["Document"]
    if metadata.get("DefaultVersion") != version:
        raise RuntimeError("SSM document default version changed during verification")
    return version


def _acquire_lock(ddb, owner: str) -> None:
    now = int(time.time())
    try:
        ddb.put_item(
            TableName=LOCK_TABLE,
            Item={
                "alert_key": {"S": LOCK_KEY},
                "owner": {"S": owner},
                "expires_at": {"N": str(now + LOCK_SECONDS)},
            },
            ConditionExpression=(
                "attribute_not_exists(alert_key) OR expires_at < :now"
            ),
            ExpressionAttributeValues={":now": {"N": str(now)}},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise RuntimeError("Another database rotation stack deployment holds the lock")
        raise


def _release_lock(ddb, owner: str) -> None:
    try:
        ddb.delete_item(
            TableName=LOCK_TABLE,
            Key={"alert_key": {"S": LOCK_KEY}},
            ConditionExpression="owner = :owner",
            ExpressionAttributeValues={":owner": {"S": owner}},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


def _renew_lock(ddb, owner: str) -> None:
    now = int(time.time())
    try:
        ddb.update_item(
            TableName=LOCK_TABLE,
            Key={"alert_key": {"S": LOCK_KEY}},
            UpdateExpression="SET expires_at = :expires",
            ConditionExpression="owner = :owner AND expires_at >= :now",
            ExpressionAttributeValues={
                ":owner": {"S": owner},
                ":now": {"N": str(now)},
                ":expires": {"N": str(now + LOCK_SECONDS)},
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise RuntimeError("Database rotation deployment lock was lost")
        raise


def deploy(
    *,
    stack_name: str,
    instance_id: str,
    secret_arn: str,
    alert_email: str,
    region: str,
    revision: str,
    template_body: str,
    expected_revision: str | None,
    adopt_unversioned_stack: bool = False,
) -> None:
    if stack_name != STACK_NAME:
        raise ValueError(f"Stack name must be {STACK_NAME}")
    session = _session()
    cloudformation = session.client("cloudformation", region_name=region)
    ssm = session.client("ssm", region_name=region)
    ddb = session.client("dynamodb", region_name=region)
    owner = uuid.uuid4().hex
    initial = _describe_stack(cloudformation, stack_name)
    lock_acquired = initial is not None
    if lock_acquired:
        _acquire_lock(ddb, owner)
    try:
        before = _describe_stack(cloudformation, stack_name)
        if initial is None and before is not None:
            raise RuntimeError("Stack appeared during bootstrap; rerun with its revision")
        if before is not None:
            status = str(before["StackStatus"])
            if status.endswith("_IN_PROGRESS"):
                raise RuntimeError(f"Stack update is already in progress ({status})")
            deployed_revision = _outputs(before).get(REVISION_OUTPUT)
            if deployed_revision is None:
                if not adopt_unversioned_stack:
                    raise RuntimeError(
                        "Existing stack has no revision fence; rerun once with "
                        "--adopt-unversioned-stack from the administrator-controlled "
                        "infrastructure pipeline"
                    )
                if expected_revision is not None:
                    raise RuntimeError(
                        "Do not combine --adopt-unversioned-stack with "
                        "--expected-current-revision"
                    )
            elif adopt_unversioned_stack:
                raise RuntimeError(
                    "--adopt-unversioned-stack is only valid before the revision "
                    "output exists"
                )
            elif expected_revision is None:
                raise RuntimeError(
                    "--expected-current-revision is required for an existing stack"
                )
            elif deployed_revision != expected_revision:
                raise RuntimeError(
                    "Stale deployment rejected: expected current revision "
                    f"{expected_revision}, found {deployed_revision or 'unrecorded'}"
                )
        elif expected_revision is not None:
            raise RuntimeError("Stack does not exist; omit --expected-current-revision")

        content_hash = _document_content_hash(template_body, region, secret_arn)
        parameters = [
            {"ParameterKey": "TemplateRevision", "ParameterValue": revision},
            {"ParameterKey": "DocumentContentHash", "ParameterValue": content_hash},
            {"ParameterKey": "ProductionInstanceId", "ParameterValue": instance_id},
            {"ParameterKey": "DatabaseSecretArn", "ParameterValue": secret_arn},
            {"ParameterKey": "AlertEmail", "ParameterValue": alert_email},
        ]
        if before is not None:
            parameters.extend(
                {"ParameterKey": key, "UsePreviousValue": True}
                for key in (
                    "DeploymentUserName",
                    "ProductionInstanceRoleName",
                    "ScheduleState",
                    "AlertDeduplicationMinutes",
                )
            )
        request = {
            "StackName": stack_name,
            "TemplateBody": template_body,
            "Parameters": parameters,
            "Capabilities": ["CAPABILITY_NAMED_IAM"],
        }
        if before is None:
            cloudformation.create_stack(**request)
            waiter_name = "stack_create_complete"
        else:
            _renew_lock(ddb, owner)
            try:
                cloudformation.update_stack(**request)
            except ClientError as exc:
                if "No updates are to be performed" not in str(exc):
                    raise
            waiter_name = "stack_update_complete"

        cloudformation.get_waiter(waiter_name).wait(StackName=stack_name)
        if lock_acquired:
            _renew_lock(ddb, owner)
        after = _describe_stack(cloudformation, stack_name)
        if after is None:
            raise RuntimeError("Stack disappeared after deployment")
        outputs = _outputs(after)
        if outputs.get(REVISION_OUTPUT) != revision:
            raise RuntimeError("CloudFormation reported an unexpected deployed revision")
        if outputs.get(DOCUMENT_OUTPUT) != DOCUMENT_NAME:
            raise RuntimeError("CloudFormation reported an unexpected SSM document")
        version = _verify_document(
            ssm,
            template_body=template_body,
            region=region,
            secret_arn=secret_arn,
            revision=revision,
            content_hash=content_hash,
        )
        if lock_acquired:
            _renew_lock(ddb, owner)
        print(
            f"Database rotation stack deployed at {revision}; "
            f"SSM version {version} and content hash verified."
        )
    finally:
        if lock_acquired:
            _release_lock(ddb, owner)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--database-secret-arn", required=True)
    parser.add_argument("--alert-email", required=True)
    parser.add_argument("--region", default="ap-south-1")
    parser.add_argument("--expected-current-revision")
    parser.add_argument("--adopt-unversioned-stack", action="store_true")
    args = parser.parse_args()
    revision, template_body = _verified_repository_source()
    deploy(
        stack_name=STACK_NAME,
        instance_id=args.instance_id,
        secret_arn=args.database_secret_arn,
        alert_email=args.alert_email,
        region=args.region,
        revision=revision,
        template_body=template_body,
        expected_revision=args.expected_current_revision,
        adopt_unversioned_stack=args.adopt_unversioned_stack,
    )


if __name__ == "__main__":
    main()