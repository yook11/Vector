"""Terraform撤去後にAWS上の残存を読み取りで確認する。"""

import json
from pathlib import Path
import time

import botocore.session

from probe import ACCOUNT, LOCAL, OPTIONS, REGION, record

TERMINAL = {
    "Success",
    "Failed",
    "TimedOut",
    "Cancelled",
    "Rejected",
    "CompletedWithSuccess",
    "CompletedWithFailure",
    "Exited",
}


def pages(client, operation, key, **kwargs):
    while True:
        response = getattr(client, operation)(**kwargs)
        yield from response.get(key, [])
        if response.get("IsTruncated") is False:
            return
        token = response.get("NextToken") or response.get("Marker")
        if not token:
            return
        kwargs["NextToken" if "NextToken" in response else "Marker"] = token


def audit():
    fixture = json.loads((LOCAL / "fixture.json").read_text())
    session = botocore.session.get_session()
    session.set_config_variable("profile", "vector-test-admin")

    def client(service):
        return session.create_client(service, region_name=REGION, config=OPTIONS)

    if (
        client("sts").get_caller_identity()["Account"] != ACCOUNT
        or fixture["account_id"] != ACCOUNT
    ):
        raise RuntimeError("WrongTestAccount")
    prefix = fixture["prefix"]
    if prefix != "vector-test-auto-" + fixture["run_id"]:
        raise RuntimeError("WrongPrefix")
    vpc_id = fixture["configuration"]["network"]["vpc_id"]
    ec2 = client("ec2")
    vpc_filter = [{"Name": "vpc-id", "Values": [vpc_id]}]
    reservations = ec2.describe_instances(Filters=vpc_filter)["Reservations"]
    instances = [i for r in reservations for i in r["Instances"]]
    volumes = {}
    for filters in [
        [{"Name": "tag:vector:managed-bastion", "Values": [prefix]}],
        [{"Name": "tag:Name", "Values": [prefix + "-outside"]}],
    ]:
        for volume in ec2.describe_volumes(Filters=filters)["Volumes"]:
            volumes[volume["VolumeId"]] = volume
    ssm = client("ssm")
    histories = list(
        pages(ssm, "describe_automation_executions", "AutomationExecutionMetadataList")
    )
    active = [
        e
        for e in histories
        if e["DocumentName"].startswith(prefix)
        and e["AutomationExecutionStatus"] not in TERMINAL
    ]
    sessions = list(pages(ssm, "describe_sessions", "Sessions", State="Active"))
    instance_ids = {i["InstanceId"] for i in instances}
    sessions = [
        s
        for s in sessions
        if s.get("Target") in instance_ids
        or s.get("DocumentName", "").startswith(prefix)
    ]
    docs = list(
        pages(
            ssm,
            "list_documents",
            "DocumentIdentifiers",
            Filters=[{"Key": "Owner", "Values": ["Self"]}],
        )
    )
    iam = client("iam")
    roles = list(
        pages(iam, "list_roles", "Roles", PathPrefix="/vector-test/bastion-automation/")
    )
    profiles = list(pages(iam, "list_instance_profiles", "InstanceProfiles"))
    counts = {
        "active_instances": sum(i["State"]["Name"] != "terminated" for i in instances),
        "volumes": len(volumes),
        "network_interfaces": len(
            ec2.describe_network_interfaces(Filters=vpc_filter)["NetworkInterfaces"]
        ),
        "endpoints": len(
            ec2.describe_vpc_endpoints(Filters=vpc_filter)["VpcEndpoints"]
        ),
        "subnets": len(ec2.describe_subnets(Filters=vpc_filter)["Subnets"]),
        "security_groups": len(
            ec2.describe_security_groups(Filters=vpc_filter)["SecurityGroups"]
        ),
        "vpcs": len(ec2.describe_vpcs(Filters=vpc_filter)["Vpcs"]),
        "roles": sum(r["RoleName"].startswith(prefix) for r in roles),
        "profiles": sum(p["InstanceProfileName"].startswith(prefix) for p in profiles),
        "documents": sum(d["Name"].startswith(prefix) for d in docs),
        "active_automations": len(active),
        "active_sessions": len(sessions),
        "queues": len(
            client("sqs").list_queues(QueueNamePrefix=prefix).get("QueueUrls", [])
        ),
        "launch_templates": len(
            ec2.describe_launch_templates(
                Filters=[
                    {"Name": "launch-template-name", "Values": [prefix + "-bastion"]}
                ]
            )["LaunchTemplates"]
        ),
    }
    state = Path(__file__).parent / "terraform.tfstate"
    if state.exists():
        counts["managed_state_resources"] = sum(
            r.get("mode") == "managed"
            for r in json.loads(state.read_text()).get("resources", [])
        )
    record("final_residual_audit", counts=counts, passed=not any(counts.values()))
    return not any(counts.values())


if __name__ == "__main__":
    for attempt in range(4):
        if audit():
            raise SystemExit(0)
        if attempt < 3:
            time.sleep(30)
    raise SystemExit(1)
