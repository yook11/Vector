"""実在する対象外資源と未承認版を使い、運用ロールの拒否を測定する。"""

import json
import time
import uuid

from probe import Probe, record


def run():
    p = Probe()
    c = p.config
    ec2 = p.admin("ec2")
    outside = None
    try:
        response = ec2.run_instances(
            ImageId=c["ami"],
            InstanceType="t4g.nano",
            MinCount=1,
            MaxCount=1,
            ClientToken=str(uuid.uuid4()),
            NetworkInterfaces=[
                {
                    "DeviceIndex": 0,
                    "SubnetId": c["network"]["subnet_id"],
                    "Groups": [c["network"]["security_group_id"]],
                    "AssociatePublicIpAddress": False,
                    "DeleteOnTermination": True,
                }
            ],
            MetadataOptions={"HttpTokens": "required"},
            BlockDeviceMappings=[
                {
                    "DeviceName": "/dev/xvda",
                    "Ebs": {
                        "VolumeSize": 8,
                        "VolumeType": "gp3",
                        "Encrypted": True,
                        "DeleteOnTermination": True,
                    },
                }
            ],
            TagSpecifications=[
                {
                    "ResourceType": kind,
                    "Tags": [
                        {"Key": "Name", "Value": p.fixture["prefix"] + "-outside"},
                        {"Key": "RunId", "Value": p.fixture["run_id"]},
                    ],
                }
                for kind in ["instance", "volume"]
            ],
        )
        outside = response["Instances"][0]["InstanceId"]
        record("outside_instance_created", instance_id=outside)
        result = p.wait(p.start("destroy", outside))
        if result["AutomationExecutionStatus"] != "Failed":
            raise RuntimeError("OutsideDestroyed")
        state = ec2.describe_instances(InstanceIds=[outside])["Reservations"][0][
            "Instances"
        ][0]["State"]["Name"]
        if state not in {"pending", "running"}:
            raise RuntimeError("OutsideTerminated")
        record("outside_destroy_rejected", passed=True, instance_id=outside)
        p.denied(
            "untagged_session",
            "ssm",
            "start_session",
            Target=outside,
            DocumentName=p.fixture["tunnel_document"],
        )
        ec2.create_tags(
            Resources=[outside],
            Tags=[
                {"Key": "vector:session-purpose", "Value": "wrong-purpose"},
                {"Key": "vector:managed-bastion", "Value": p.fixture["prefix"]},
            ],
        )
        p.denied(
            "wrong_tag_session",
            "ssm",
            "start_session",
            Target=outside,
            DocumentName=p.fixture["tunnel_document"],
        )
    finally:
        if outside:
            ec2.terminate_instances(InstanceIds=[outside])
            ec2.get_waiter("instance_terminated").wait(
                InstanceIds=[outside], WaiterConfig={"Delay": 5, "MaxAttempts": 90}
            )
            record("outside_instance_removed", instance_id=outside)

    ssm = p.admin("ssm")
    source = ssm.get_document(
        Name=c["create_document"], DocumentVersion=c["create_version"]
    )
    content = json.loads(source["Content"])
    content["description"] = "Unapproved test revision; fixed behavior is unchanged"
    revision = None
    try:
        revision = ssm.update_document(
            Name=c["create_document"],
            DocumentVersion="$LATEST",
            DocumentFormat="JSON",
            Content=json.dumps(content),
        )["DocumentDescription"]["DocumentVersion"]
        for _ in range(20):
            if (
                ssm.describe_document(
                    Name=c["create_document"], DocumentVersion=revision
                )["Document"]["Status"]
                == "Active"
            ):
                break
            time.sleep(1)
        p.denied(
            "existing_unapproved_numeric_version",
            "ssm",
            "start_automation_execution",
            DocumentName=c["create_document"],
            DocumentVersion=revision,
        )
        p.denied(
            "unapproved_latest_alias",
            "ssm",
            "start_automation_execution",
            DocumentName=c["create_document"],
            DocumentVersion="$LATEST",
        )
        ssm.update_document_default_version(
            Name=c["create_document"], DocumentVersion=revision
        )
        p.denied(
            "unapproved_default_alias",
            "ssm",
            "start_automation_execution",
            DocumentName=c["create_document"],
            DocumentVersion="$DEFAULT",
        )
        p.denied(
            "unapproved_omitted_version",
            "ssm",
            "start_automation_execution",
            DocumentName=c["create_document"],
        )
    finally:
        if revision:
            ssm.update_document_default_version(
                Name=c["create_document"], DocumentVersion=c["create_version"]
            )
            ssm.delete_document(Name=c["create_document"], DocumentVersion=revision)
            record("unapproved_revision_removed", version=revision)


if __name__ == "__main__":
    run()
