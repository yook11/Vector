"""削除結果をAWSの残存照会と照合し、確認不能を成功にしない。"""

import json
import time

from botocore.exceptions import ClientError

from .common import client, save


def inventory(aws, run_id, known=None):
    prefix = "vector-test-" + run_id
    remaining = {}
    filters = [
        {"Name": "tag:Project", "Values": ["vector-test"]},
        {"Name": "tag:RunId", "Values": [run_id]},
    ]
    with client(aws, "ec2") as ec2:
        for method, key, id_field, id_filter in [
            ("describe_vpcs", "Vpcs", "VpcId", "vpc-id"),
            ("describe_subnets", "Subnets", "SubnetId", "subnet-id"),
            ("describe_route_tables", "RouteTables", "RouteTableId", "route-table-id"),
            (
                "describe_internet_gateways",
                "InternetGateways",
                "InternetGatewayId",
                "internet-gateway-id",
            ),
            ("describe_security_groups", "SecurityGroups", "GroupId", "group-id"),
            (
                "describe_vpc_endpoints",
                "VpcEndpoints",
                "VpcEndpointId",
                "vpc-endpoint-id",
            ),
            ("describe_volumes", "Volumes", "VolumeId", "volume-id"),
        ]:
            searches = [filters]
            if ids := [i[id_field] for i in (known or {}).get(key, [])]:
                searches.append([{"Name": id_filter, "Values": sorted(set(ids))}])
            found = {}
            for query in searches:
                for page in ec2.get_paginator(method).paginate(Filters=query):
                    found.update({i[id_field]: i for i in page[key]})
            remaining[key] = list(found.values())
        remaining["Instances"] = [
            i
            for page in ec2.get_paginator("describe_instances").paginate(
                Filters=filters
            )
            for reservation in page["Reservations"]
            for i in reservation["Instances"]
            if i["State"]["Name"] != "terminated"
        ]
        if ids := [i["InstanceId"] for i in (known or {}).get("Instances", [])]:
            found = {i["InstanceId"]: i for i in remaining["Instances"]}
            for page in ec2.get_paginator("describe_instances").paginate(
                Filters=[{"Name": "instance-id", "Values": sorted(set(ids))}]
            ):
                found.update(
                    {
                        i["InstanceId"]: i
                        for r in page["Reservations"]
                        for i in r["Instances"]
                        if i["State"]["Name"] != "terminated"
                    }
                )
            remaining["Instances"] = list(found.values())
        vpcs = {v["VpcId"] for v in remaining["Vpcs"]}
        if known:
            vpcs.update(v["VpcId"] for v in known.get("Vpcs", []))
        remaining["NetworkInterfaces"] = []
        for vpc in vpcs:
            remaining["NetworkInterfaces"].extend(
                i
                for page in ec2.get_paginator("describe_network_interfaces").paginate(
                    Filters=[{"Name": "vpc-id", "Values": [vpc]}]
                )
                for i in page["NetworkInterfaces"]
            )
    with client(aws, "rds") as rds:
        for method, key, field in [
            ("describe_db_instances", "DBInstances", "DBInstanceIdentifier"),
            ("describe_db_subnet_groups", "DBSubnetGroups", "DBSubnetGroupName"),
            (
                "describe_db_parameter_groups",
                "DBParameterGroups",
                "DBParameterGroupName",
            ),
        ]:
            remaining[key] = [
                i
                for page in rds.get_paginator(method).paginate()
                for i in page[key]
                if i[field] == prefix
            ]
        remaining["DBSnapshots"] = [
            i
            for page in rds.get_paginator("describe_db_snapshots").paginate()
            for i in page["DBSnapshots"]
            if i["DBInstanceIdentifier"] == prefix
        ]
        remaining["DBInstanceAutomatedBackups"] = [
            i
            for page in rds.get_paginator(
                "describe_db_instance_automated_backups"
            ).paginate()
            for i in page["DBInstanceAutomatedBackups"]
            if i["DBInstanceIdentifier"] == prefix
        ]

    def exists(service, method, parameters, missing):
        with client(aws, service) as api:
            try:
                getattr(api, method)(**parameters)
                return True
            except ClientError as error:
                if error.response["Error"]["Code"] not in missing:
                    raise
                return False

    remaining["Lambda"] = (
        [prefix]
        if exists(
            "lambda",
            "get_function",
            {"FunctionName": prefix + "-embedding"},
            {"ResourceNotFoundException"},
        )
        else []
    )
    remaining["Queue"] = (
        [prefix]
        if exists(
            "sqs",
            "get_queue_url",
            {"QueueName": prefix + "-embedding"},
            {"AWS.SimpleQueueService.NonExistentQueue", "QueueDoesNotExist"},
        )
        else []
    )
    remaining["Roles"] = [
        kind
        for kind in ["lambda", "runner", "proxy"]
        if exists(
            "iam", "get_role", {"RoleName": prefix + "-" + kind}, {"NoSuchEntity"}
        )
    ]
    remaining["Profiles"] = [
        kind
        for kind in ["runner", "proxy"]
        if exists(
            "iam",
            "get_instance_profile",
            {"InstanceProfileName": prefix + "-" + kind},
            {"NoSuchEntity"},
        )
    ]
    remaining["Logs"] = []
    with client(aws, "logs") as logs:
        for path in [f"/vector-test/{prefix}/", f"/aws/rds/instance/{prefix}/"]:
            remaining["Logs"].extend(
                g["logGroupName"]
                for page in logs.get_paginator("describe_log_groups").paginate(
                    logGroupNamePrefix=path
                )
                for g in page["logGroups"]
            )
    secrets = {
        i["MasterUserSecret"]["SecretArn"]
        for i in remaining["DBInstances"]
        if i.get("MasterUserSecret")
    }
    if known:
        secrets.update(known.get("Secrets", []))
    remaining["Secrets"] = [
        arn
        for arn in secrets
        if exists(
            "secretsmanager",
            "describe_secret",
            {"SecretId": arn},
            {"ResourceNotFoundException"},
        )
    ]
    remaining["EventSourceMappings"] = [
        uuid
        for uuid in (known or {}).get("EventSourceMappings", [])
        if exists(
            "lambda",
            "get_event_source_mapping",
            {"UUID": uuid},
            {"ResourceNotFoundException"},
        )
    ]
    return remaining


def remember_state(state, known):
    def resources(module):
        yield from module.get("resources", [])
        for child in module.get("child_modules", []):
            yield from resources(child)

    for resource in resources(state.get("values", {}).get("root_module", {})):
        value = resource["values"]
        kinds = {
            "aws_vpc": ("Vpcs", "VpcId"),
            "aws_subnet": ("Subnets", "SubnetId"),
            "aws_route_table": ("RouteTables", "RouteTableId"),
            "aws_internet_gateway": ("InternetGateways", "InternetGatewayId"),
            "aws_security_group": ("SecurityGroups", "GroupId"),
            "aws_vpc_endpoint": ("VpcEndpoints", "VpcEndpointId"),
            "aws_instance": ("Instances", "InstanceId"),
        }
        if resource["type"] in kinds and value.get("id"):
            key, field = kinds[resource["type"]]
            known.setdefault(key, []).append({field: value["id"]})
        if resource["type"] == "aws_instance":
            known.setdefault("Volumes", []).extend(
                {"VolumeId": v["volume_id"]}
                for v in value.get("root_block_device", [])
                if v.get("volume_id")
            )
        if resource["type"] == "aws_db_instance":
            known.setdefault("Secrets", []).extend(
                s["secret_arn"] for s in value.get("master_user_secret", [])
            )
        elif resource["type"] == "aws_lambda_event_source_mapping":
            known.setdefault("EventSourceMappings", []).append(value["uuid"])
    return known


def verify_deleted(aws, run_id, known, directory):
    deadline = time.monotonic() + 180
    while True:
        remaining = inventory(aws, run_id, known)
        save(directory / "remaining.json", remaining)
        if not any(remaining.values()):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("resources_remain_after_destroy")
        time.sleep(10)


def collect_logs(aws, outputs, directory):
    deadline = time.monotonic() + 120
    collected = {}
    with client(aws, "logs") as logs:
        for kind, group in outputs["execution"]["log_groups"].items():
            try:
                with (directory / f"{kind}.jsonl").open("w") as output:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("log_collection_timeout")
                    for page in logs.get_paginator("filter_log_events").paginate(
                        logGroupName=group
                    ):
                        if time.monotonic() >= deadline:
                            raise TimeoutError("log_collection_timeout")
                        for event in page["events"]:
                            output.write(json.dumps(event, ensure_ascii=False) + "\n")
                collected[kind] = {"status": "collected"}
            except Exception as error:
                collected[kind] = {
                    "status": "unconfirmed",
                    "error_type": type(error).__name__,
                }
            finally:
                save(directory / "log-collection.json", collected)
    if any(v["status"] != "collected" for v in collected.values()):
        raise RuntimeError("log_collection_incomplete")
