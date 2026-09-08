#!/usr/bin/env python3
"""Guarded, disposable AWS rehearsal of the database secret refresh path.

This is intentionally opt-in: it creates an RDS instance and never accepts,
prints, or stores a database password.  It must be run only in the supplied
non-production account.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone

from install_openai_fallback_via_ssm import _session

TAG_KEY = "university-portal:disposable-rehearsal"
PRODUCTION_ACCOUNT_IDS = frozenset({"905043442097"})
SUCCESS_OUTPUT = "database-credentials-refreshed-and-db-verified"


def _tags(tags: list[dict[str, str]]) -> dict[str, str]:
    return {tag["Key"]: tag["Value"] for tag in tags}


def _account(session, region: str) -> str:
    return session.client("sts", region_name=region).get_caller_identity()["Account"]


def _require_disposable(session, region: str, expected_account: str, run_id: str, resource_tags) -> None:
    """Fail closed before mutation, using the exact credential session."""
    actual = _account(session, region)
    if actual != expected_account or actual in PRODUCTION_ACCOUNT_IDS:
        raise RuntimeError("refusing AWS mutation outside the explicit non-production account")
    if _tags(resource_tags).get(TAG_KEY) != run_id:
        raise RuntimeError("refusing mutation of resource without this run's ownership tag")


def _managed_secret_shape(secrets, arn: str) -> dict[str, object]:
    """Validate RDS JSON without printing, returning, or persisting its values."""
    metadata = secrets.describe_secret(SecretId=arn)
    if not metadata.get("RotationEnabled") and not metadata.get("OwningService", "").startswith("rds"):
        raise RuntimeError("fixture secret is not an RDS-managed secret")
    if not metadata.get("ARN") or not metadata.get("VersionIdsToStages"):
        raise RuntimeError("fixture managed secret has an unexpected shape")
    value = json.loads(secrets.get_secret_value(SecretId=arn)["SecretString"])
    if not all(isinstance(value.get(key), str) and value[key] for key in ("username", "password")):
        raise RuntimeError("fixture managed secret lacks username/password")
    # RDS returns endpoint fields today, but production deliberately derives absent
    # endpoint fields from the protected existing DATABASE_URL.
    for key in ("host", "dbname"):
        if key in value and not isinstance(value[key], str):
            raise RuntimeError("fixture managed secret has invalid optional endpoint fields")
    if "port" in value and (not isinstance(value["port"], int) or not 1 <= value["port"] <= 65535):
        raise RuntimeError("fixture managed secret has invalid optional port")
    return metadata


def _wait_command(ssm, instance_id: str, document: str, expected: str) -> None:
    response = ssm.send_command(
        InstanceIds=[instance_id], DocumentName=document,
        Comment="prepare disposable database refresh rehearsal",
    )
    command_id = response["Command"]["CommandId"]
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            result = ssm.get_command_invocation(
                CommandId=command_id, InstanceId=instance_id
            )
        except ssm.exceptions.InvocationDoesNotExist:
            time.sleep(2)
            continue
        if result["Status"] in {"Pending", "InProgress", "Delayed"}:
            time.sleep(2)
            continue
        if result["Status"] != "Success" or result.get(
            "StandardOutputContent", ""
        ).strip() != expected:
            raise RuntimeError("fixed rehearsal preparation command failed")
        return
    raise TimeoutError("fixed rehearsal preparation command timed out")


def rehearse(*, expected_account: str, production_account: str, region: str, vpc_id: str,
             private_subnet_id: str, second_private_subnet_id: str, stack_name: str,
             opt_in: bool) -> None:
    if not opt_in:
        raise RuntimeError("pass --i-understand-this-creates-disposable-aws-resources")
    if os.environ.get("RUN_DISPOSABLE_AWS_DATABASE_REFRESH_REHEARSAL") != "1":
        raise RuntimeError(
            "set RUN_DISPOSABLE_AWS_DATABASE_REFRESH_REHEARSAL=1 explicitly"
        )
    session = _session()
    # Account identity is checked before CloudFormation, Scheduler, SSM, RDS, and SQS mutations.
    if (not production_account or expected_account == production_account
            or _account(session, region) != expected_account
            or expected_account in PRODUCTION_ACCOUNT_IDS):
        raise RuntimeError("expected account is missing, mismatched, or configured as production")
    cf = session.client("cloudformation", region_name=region)
    match = re.fullmatch(r"up-db-refresh-rehearsal-([0-9a-f]{16})", stack_name)
    if not match:
        raise RuntimeError("stack name is generated from the unique rehearsal run ID; do not supply a shared name")
    rehearsal_id = match.group(1)
    ec2 = session.client("ec2", region_name=region)
    vpcs = ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"]
    if (
        len(vpcs) != 1
        or vpcs[0].get("IsDefault")
        or _tags(vpcs[0].get("Tags", [])).get(
            "university-portal:disposable-network"
        )
        != expected_account
    ):
        raise RuntimeError("VPC must be non-default and explicitly disposable-owned")
    subnets = ec2.describe_subnets(SubnetIds=[private_subnet_id, second_private_subnet_id])["Subnets"]
    if (len(subnets) != 2 or any(s["VpcId"] != vpc_id or s.get("MapPublicIpOnLaunch") for s in subnets)
            or subnets[0]["AvailabilityZone"] == subnets[1]["AvailabilityZone"]
            or any(_tags(s.get("Tags", [])).get("university-portal:disposable-network") != expected_account for s in subnets)):
        raise RuntimeError("subnets must be distinct-AZ, private, and explicitly disposable-owned")
    route_tables = ec2.describe_route_tables(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
    )["RouteTables"]
    routed = set()
    for subnet_id in (private_subnet_id, second_private_subnet_id):
        table = next(
            (
                candidate
                for candidate in route_tables
                if any(
                    association.get("SubnetId") == subnet_id
                    for association in candidate.get("Associations", [])
                )
            ),
            next(
                (
                    candidate
                    for candidate in route_tables
                    if any(
                        association.get("Main")
                        for association in candidate.get("Associations", [])
                    )
                ),
                None,
            ),
        )
        if table and any(
            route.get("DestinationCidrBlock") == "0.0.0.0/0"
            and route.get("NatGatewayId")
            and route.get("State") == "active"
            for route in table.get("Routes", [])
        ):
            routed.add(subnet_id)
    if routed != {private_subnet_id, second_private_subnet_id}:
        raise RuntimeError("each private subnet must have an active default NAT gateway route")
    template_url = "file://" + __file__.replace("rehearse_database_secret_refresh.py", "database-secret-refresh-rehearsal.yaml")
    # boto3 does not accept local TemplateURL; read fixture without interpolating secrets.
    with open(template_url[7:], encoding="utf-8") as source:
        body = source.read()
    created = False
    outputs: dict[str, str] = {}
    primary_error: BaseException | None = None
    try:
        _require_account(session, region, expected_account, production_account)
        created = True
        cf.create_stack(StackName=stack_name, TemplateBody=body, Capabilities=["CAPABILITY_IAM"],
                        Tags=[
                            {"Key": "System", "Value": "university-portal"},
                            {"Key": "Purpose", "Value": "database-secret-refresh-rehearsal"},
                            {"Key": TAG_KEY, "Value": rehearsal_id},
                        ],
                        Parameters=[{"ParameterKey": "RehearsalId", "ParameterValue": rehearsal_id},
                                    {"ParameterKey": "VpcId", "ParameterValue": vpc_id},
                                    {"ParameterKey": "PrivateSubnetId", "ParameterValue": private_subnet_id},
                                    {"ParameterKey": "SecondPrivateSubnetId", "ParameterValue": second_private_subnet_id},
                                    {"ParameterKey": "ExpectedAccountId", "ParameterValue": expected_account}])
        cf.get_waiter("stack_create_complete").wait(StackName=stack_name)
        resources = cf.describe_stack_resources(StackName=stack_name)["StackResources"]
        tagged = {r["LogicalResourceId"]: r["PhysicalResourceId"] for r in resources}
        host = tagged["Host"]
        _require_disposable(session, region, expected_account, rehearsal_id,
                            ec2.describe_tags(Filters=[{"Name": "resource-id", "Values": [host]}])["Tags"])
        outputs = {o["OutputKey"]: o["OutputValue"] for o in cf.describe_stacks(StackName=stack_name)["Stacks"][0]["Outputs"]}
        secrets = session.client("secretsmanager", region_name=region)
        _require_account(session, region, expected_account, production_account)
        before = _managed_secret_shape(secrets, outputs["DatabaseSecretArn"])
        ssm = session.client("ssm", region_name=region)
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            info = ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": [host]}]
            )["InstanceInformationList"]
            if info and info[0]["PingStatus"] == "Online":
                break
            time.sleep(10)
        else:
            raise TimeoutError("rehearsal host never became SSM Online")
        _require_disposable(
            session, region, expected_account, rehearsal_id,
            ec2.describe_tags(Filters=[{"Name": "resource-id", "Values": [host]}])["Tags"],
        )
        # First invocation is deliberately before rotation/queueing: the generated
        # environment already has AWSCURRENT, so the production cmp guard proves
        # the unchanged-version no-restart path.
        _wait_command(ssm, host, outputs["RefreshDocument"], SUCCESS_OUTPUT)
        _require_disposable(
            session,
            region,
            expected_account,
            rehearsal_id,
            ec2.describe_tags(
                Filters=[{"Name": "resource-id", "Values": [host]}]
            )["Tags"],
        )
        _wait_command(ssm, host, outputs["PrepareDocument"], "rehearsal-probe-queued")
        rds = session.client("rds", region_name=region)
        db = rds.describe_db_instances(DBInstanceIdentifier=outputs["DatabaseId"])["DBInstances"][0]
        _require_disposable(session, region, expected_account, rehearsal_id,
                            rds.list_tags_for_resource(ResourceName=db["DBInstanceArn"])["TagList"])
        rotation_started = datetime.now(timezone.utc)
        rds.modify_db_instance(DBInstanceIdentifier=outputs["DatabaseId"], RotateMasterUserPassword=True, ApplyImmediately=True)
        rds.get_waiter("db_instance_available").wait(DBInstanceIdentifier=outputs["DatabaseId"])
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            after = _managed_secret_shape(secrets, outputs["DatabaseSecretArn"])
            if after["VersionIdsToStages"] != before["VersionIdsToStages"]:
                break
            time.sleep(10)
        else:
            raise TimeoutError("managed-secret rotation did not expose a new version")
        # Scheduler is the execution path: require a command requested after
        # rotation began, with the fixed document and scheduler-only comment.
        command = None
        deadline = time.monotonic() + 660
        while time.monotonic() < deadline:
            commands = ssm.list_commands(InstanceId=host, MaxResults=50)["Commands"]
            evidence = [
                candidate
                for candidate in commands
                if candidate["DocumentName"] == outputs["RefreshDocument"]
                and "scheduler-origin" in candidate.get("Comment", "")
                and candidate.get("RequestedDateTime", rotation_started)
                >= rotation_started
            ]
            if evidence:
                command = max(
                    evidence,
                    key=lambda candidate: candidate.get(
                        "RequestedDateTime", rotation_started
                    ),
                )
                break
            time.sleep(10)
        if command is None:
            raise RuntimeError("no post-rotation Scheduler-origin SSM command evidence")
        while time.monotonic() < deadline:
            invocation = ssm.get_command_invocation(
                CommandId=command["CommandId"], InstanceId=host
            )
            if invocation["Status"] not in {"Pending", "InProgress", "Delayed"}:
                break
            time.sleep(5)
        if invocation["Status"] != "Success" or invocation.get("StandardOutputContent", "").strip() != SUCCESS_OUTPUT:
            raise RuntimeError("rehearsal refresh/restart/TLS assertion failed")
        print("disposable-database-secret-refresh-rehearsal-passed")
    except BaseException as error:
        primary_error = error
        raise
    finally:
        # Disable delivery first: no command may be scheduled while teardown starts.
        cleanup_errors: list[str] = []
        if created:
            if outputs.get("ScheduleName") and outputs.get("ScheduleGroup"):
                try:
                    scheduler = session.client("scheduler", region_name=region)
                    schedule = scheduler.get_schedule(
                        Name=outputs["ScheduleName"],
                        GroupName=outputs["ScheduleGroup"],
                    )
                    _require_account(
                        session, region, expected_account, production_account
                    )
                    scheduler.update_schedule(
                        Name=outputs["ScheduleName"],
                        GroupName=outputs["ScheduleGroup"],
                        State="DISABLED",
                        ScheduleExpression=schedule["ScheduleExpression"],
                        FlexibleTimeWindow=schedule["FlexibleTimeWindow"],
                        Target=schedule["Target"],
                    )
                except Exception as cleanup_error:
                    cleanup_errors.append(
                        f"disable scheduler failed: {cleanup_error}"
                    )
            try:
                _require_account(session, region, expected_account, production_account)
                cf.delete_stack(StackName=stack_name)
                cf.get_waiter("stack_delete_complete").wait(StackName=stack_name)
            except Exception as cleanup_error:
                cleanup_errors.append(f"stack deletion failed: {cleanup_error}")
            try:
                _require_account(session, region, expected_account, production_account)
                tagging = session.client("resourcegroupstaggingapi", region_name=region)
                residue_deadline = time.monotonic() + 120
                while True:
                    residues = []
                    token = ""
                    while True:
                        request = {
                            "TagFilters": [
                                {"Key": TAG_KEY, "Values": [rehearsal_id]}
                            ]
                        }
                        if token:
                            request["PaginationToken"] = token
                        page = tagging.get_resources(**request)
                        residues.extend(page["ResourceTagMappingList"])
                        token = page.get("PaginationToken", "")
                        if not token:
                            break
                    if not residues or time.monotonic() >= residue_deadline:
                        break
                    time.sleep(5)
                if residues:
                    cleanup_errors.append(
                        f"{len(residues)} run-tagged AWS resources remain"
                    )
            except Exception as cleanup_error:
                cleanup_errors.append(f"residue verification failed: {cleanup_error}")
            if outputs.get("DatabaseSecretArn"):
                try:
                    secrets.describe_secret(SecretId=outputs["DatabaseSecretArn"])
                    cleanup_errors.append("RDS managed secret remains after teardown")
                except Exception as cleanup_error:
                    code = getattr(cleanup_error, "response", {}).get(
                        "Error", {}
                    ).get("Code")
                    if code != "ResourceNotFoundException":
                        cleanup_errors.append(
                            f"secret residue verification failed: {cleanup_error}"
                        )
        if cleanup_errors:
            message = "rehearsal cleanup failed: " + "; ".join(cleanup_errors)
            if primary_error is not None:
                primary_error.add_note(message)
            else:
                raise RuntimeError(message)


def _require_account(session, region: str, expected: str, production: str) -> None:
    if not production or expected == production or _account(session, region) != expected or expected in PRODUCTION_ACCOUNT_IDS:
        raise RuntimeError("expected account is missing, mismatched, or configured as production")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-account-id", required=True)
    parser.add_argument("--production-account-id", required=True,
                        help="production account rejected by this rehearsal")
    parser.add_argument("--vpc-id", required=True)
    parser.add_argument("--private-subnet-id", required=True)
    parser.add_argument("--second-private-subnet-id", required=True)
    parser.add_argument("--region", default="ap-south-1")
    parser.add_argument("--i-understand-this-creates-disposable-aws-resources", action="store_true")
    args = parser.parse_args()
    run_id = uuid.uuid4().hex[:16]
    rehearse(expected_account=args.expected_account_id, production_account=args.production_account_id,
             region=args.region, vpc_id=args.vpc_id,
             private_subnet_id=args.private_subnet_id, second_private_subnet_id=args.second_private_subnet_id, stack_name=f"up-db-refresh-rehearsal-{run_id}",
             opt_in=args.i_understand_this_creates_disposable_aws_resources)


if __name__ == "__main__":
    main()