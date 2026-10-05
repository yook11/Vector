"""destroy後に試験リソースと接続の残存を読み取り専用で確認する。"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

from probe import ACCOUNT, ROOT, AwsFailure, aws


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    run = json.loads(args.record.read_text())
    fixture = run["fixture"]
    prefix = fixture["prefix"]
    if (
        not re.fullmatch(r"vector-test-dlq-[a-z0-9][a-z0-9-]{0,23}", prefix)
        or prefix != f"vector-test-dlq-{run['run_id']}"
    ):
        raise RuntimeError("試験prefixが不正です。")
    identity = aws("vector-test-admin", "sts", "get-caller-identity")
    if identity["Account"] != ACCOUNT or fixture["account_id"] != ACCOUNT:
        raise RuntimeError("試験アカウントではありません。")
    state = subprocess.check_output(
        ["terraform", f"-chdir={ROOT}", "state", "list"], text=True
    ).strip()
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "account_id": ACCOUNT,
        "prefix": prefix,
        "state_empty": not state,
    }
    report["queues"] = len(
        aws("vector-test-admin", "sqs", "list-queues", queue_name_prefix=prefix).get(
            "QueueUrls", []
        )
    )
    roles = aws(
        "vector-test-admin",
        "iam",
        "list-roles",
        path_prefix="/vector-test/dlq-redrive/",
    )["Roles"]
    report["roles"] = sum(role["RoleName"].startswith(prefix + "-") for role in roles)
    profiles = aws("vector-test-admin", "iam", "list-instance-profiles")[
        "InstanceProfiles"
    ]
    report["instance_profiles"] = sum(
        profile["InstanceProfileName"].startswith(prefix + "-") for profile in profiles
    )
    filters = json.dumps([{"Name": "tag:RunId", "Values": [run["run_id"]]}])
    for label, operation, key in (
        ("vpcs", "describe-vpcs", "Vpcs"),
        ("vpc_endpoints", "describe-vpc-endpoints", "VpcEndpoints"),
        ("security_groups", "describe-security-groups", "SecurityGroups"),
        ("subnets", "describe-subnets", "Subnets"),
    ):
        report[label] = len(
            aws("vector-test-admin", "ec2", operation, filters=filters)[key]
        )
    instances = aws("vector-test-admin", "ec2", "describe-instances", filters=filters)[
        "Reservations"
    ]
    report["non_terminated_instances"] = sum(
        instance["State"]["Name"] != "terminated"
        for reservation in instances
        for instance in reservation["Instances"]
    )
    document = fixture["private_connection"]["document_name"]
    if document != f"{prefix}-sqs-tunnel":
        raise RuntimeError("試験ドキュメントが不正です。")
    try:
        aws("vector-test-admin", "ssm", "describe-document", name=document)
        report["ssm_documents"] = 1
    except AwsFailure as error:
        if error.code != "InvalidDocument":
            raise
        report["ssm_documents"] = 0
    report["root_volumes"] = 0
    for volume in run["root_volumes"]:
        if not re.fullmatch(r"vol-[0-9a-f]+", volume):
            raise RuntimeError("試験volume IDが不正です。")
        try:
            aws("vector-test-admin", "ec2", "describe-volumes", volume_ids=volume)
            report["root_volumes"] += 1
        except AwsFailure as error:
            if error.code != "InvalidVolume.NotFound":
                raise
    report["active_sessions"] = 0
    instance_ids = {
        instance["InstanceId"]
        for reservation in instances
        for instance in reservation["Instances"]
    }
    for instance_id in instance_ids:
        sessions = aws(
            "vector-test-admin",
            "ssm",
            "describe-sessions",
            state="Active",
            filters=json.dumps([{"key": "Target", "value": instance_id}]),
        )["Sessions"]
        report["active_sessions"] += len(sessions)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    counters = [
        "queues",
        "roles",
        "instance_profiles",
        "vpcs",
        "vpc_endpoints",
        "security_groups",
        "subnets",
        "non_terminated_instances",
        "ssm_documents",
        "root_volumes",
        "active_sessions",
    ]
    if not report["state_empty"] or any(report[key] for key in counters):
        raise RuntimeError("試験リソースが残っています。")


if __name__ == "__main__":
    main()
