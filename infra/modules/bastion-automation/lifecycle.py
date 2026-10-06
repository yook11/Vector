"""固定ENIと実行IDを照合し、対象踏み台だけを作成・撤去する。"""

import re
import time


class LifecycleError(RuntimeError):
    pass


def instances(ec2, **kwargs):
    return [
        instance
        for reservation in ec2.describe_instances(**kwargs).get("Reservations", [])
        for instance in reservation.get("Instances", [])
    ]


def interface(ec2, config):
    network = config["network"]
    value = ec2.describe_network_interfaces(
        NetworkInterfaceIds=[network["network_interface_id"]]
    )["NetworkInterfaces"][0]
    if (
        value.get("OwnerId") != config["account_id"]
        or value.get("VpcId") != network["vpc_id"]
        or value.get("SubnetId") != network["subnet_id"]
        or {group["GroupId"] for group in value.get("Groups", [])}
        != {network["security_group_id"]}
        or value.get("Association", {}).get("PublicIp")
    ):
        raise LifecycleError("UnexpectedInterface")
    return value


def validate_instance(instance, config, *, current=True, attached=True):
    tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
    if (
        not all(tags.get(key) == value for key, value in config["tags"].items())
        or tags.get("aws:ec2launchtemplate:id") != config["template_id"]
        or instance.get("VpcId") != config["network"]["vpc_id"]
        or instance.get("SubnetId") != config["network"]["subnet_id"]
        or instance.get("IamInstanceProfile", {}).get("Arn") != config["profile_arn"]
        or instance.get("PublicIpAddress")
    ):
        raise LifecycleError("UnexpectedInstance")
    if current and (
        tags.get("aws:ec2launchtemplate:version") != config["template_version"]
        or instance.get("ImageId") != config["ami"]
        or instance.get("InstanceType") != "t4g.nano"
        or instance.get("MetadataOptions", {}).get("HttpTokens") != "required"
    ):
        raise LifecycleError("UnexpectedLaunchConfiguration")
    if attached:
        primary = [
            eni
            for eni in instance.get("NetworkInterfaces", [])
            if eni.get("Attachment", {}).get("DeviceIndex") == 0
        ]
        if (
            len(primary) != 1
            or primary[0].get("NetworkInterfaceId")
            != config["network"]["network_interface_id"]
            or primary[0]["Attachment"].get("DeleteOnTermination") is not False
        ):
            raise LifecycleError("UnexpectedPrimaryInterface")


def existing(ec2, config):
    eni = interface(ec2, config)
    attachment = eni.get("Attachment", {})
    instance_id = attachment.get("InstanceId")
    if not instance_id:
        if eni.get("Status") != "available":
            raise LifecycleError("InterfaceTransitionInProgress")
        return None
    if attachment.get("DeviceIndex") != 0:
        raise LifecycleError("UnexpectedAttachment")
    try:
        found = instances(ec2, InstanceIds=[instance_id])
    except Exception as error:
        if (
            getattr(error, "response", {}).get("Error", {}).get("Code")
            == "InvalidInstanceID.NotFound"
        ):
            raise LifecycleError("InstanceNotVisible") from None
        raise
    if len(found) != 1:
        raise LifecycleError("InstanceNotVisible")
    validate_instance(found[0], config)
    state = found[0]["State"]["Name"]
    if state not in {"pending", "running"}:
        raise LifecycleError("InstanceState_" + state)
    return found[0]


def create(ec2, config, execution_id, *, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + 90
    requested = False
    while clock() < deadline:
        try:
            value = existing(ec2, config)
            if value:
                return {
                    "InstanceId": value["InstanceId"],
                    "Reused": not requested or value.get("ClientToken") != execution_id,
                }
            if not requested:
                requested = True
                # 応答喪失時も実行IDで照合し、新しい要求を自動発行しない。
                try:
                    ec2.run_instances(
                        LaunchTemplate={
                            "LaunchTemplateId": config["template_id"],
                            "Version": config["template_version"],
                        },
                        MinCount=1,
                        MaxCount=1,
                        ClientToken=execution_id,
                    )
                except Exception as error:
                    code = getattr(error, "response", {}).get("Error", {}).get("Code")
                    if code in {
                        "UnauthorizedOperation",
                        "InvalidParameterValue",
                        "InvalidParameterCombination",
                        "InsufficientInstanceCapacity",
                        "InstanceLimitExceeded",
                    }:
                        raise LifecycleError("LaunchRejected_" + code) from None
        except LifecycleError as error:
            if str(error) not in {
                "InstanceNotVisible",
                "InterfaceTransitionInProgress",
            }:
                raise
        sleep(3)
    raise LifecycleError("LaunchOutcomeUnknownInspectExecution")


def active_sessions(ssm, instance_id):
    arguments = {
        "State": "Active",
        "Filters": [{"key": "Target", "value": instance_id}],
    }
    while True:
        response = ssm.describe_sessions(**arguments)
        if response.get("Sessions"):
            return True
        token = response.get("NextToken")
        if not token:
            return False
        arguments["NextToken"] = token


def root_volume(instance):
    roots = [
        item["Ebs"]
        for item in instance.get("BlockDeviceMappings", [])
        if item.get("DeviceName") == instance.get("RootDeviceName") and "Ebs" in item
    ]
    if len(roots) != 1 or roots[0].get("DeleteOnTermination") is not True:
        raise LifecycleError("UnexpectedRootVolume")
    return roots[0]["VolumeId"]


def wait_destroyed(
    ec2, config, instance_id, volume_id, *, sleep=time.sleep, clock=time.monotonic
):
    deadline = clock() + 450
    while clock() < deadline:
        found = instances(
            ec2, Filters=[{"Name": "instance-id", "Values": [instance_id]}]
        )
        terminated = not found or found[0]["State"]["Name"] == "terminated"
        volumes = ec2.describe_volumes(
            Filters=[{"Name": "volume-id", "Values": [volume_id]}]
        ).get("Volumes", [])
        attachment = interface(ec2, config).get("Attachment", {})
        # 後継機が同じENIを取得しても、指定した機体の終了確認だけを行う。
        released = attachment.get("InstanceId") != instance_id
        if terminated and not volumes and released:
            return {"InstanceId": instance_id, "State": "terminated"}
        sleep(5)
    raise LifecycleError("TerminationIncompleteInspectInstance_" + instance_id)


def destroy(ec2, ssm, config, instance_id, **wait_options):
    if not re.fullmatch(r"i-[0-9a-f]{17}", instance_id):
        raise LifecycleError("InvalidInstanceId")
    found = instances(ec2, InstanceIds=[instance_id])
    if len(found) != 1:
        raise LifecycleError("InstanceNotVisible")
    value = found[0]
    state = value["State"]["Name"]
    validate_instance(value, config, current=False, attached=state != "terminated")
    if state == "terminated":
        raise LifecycleError("AlreadyTerminatedInspectExecution")
    eni = interface(ec2, config)
    if eni.get("Attachment", {}).get("InstanceId") != instance_id:
        raise LifecycleError("InstanceDoesNotOwnInterface")
    volume_id = root_volume(value)
    if active_sessions(ssm, instance_id):
        raise LifecycleError("ActiveSessionRefused")
    ec2.terminate_instances(InstanceIds=[instance_id])
    return wait_destroyed(ec2, config, instance_id, volume_id, **wait_options)


def cleanup(ec2, ssm, config, execution_id, **wait_options):
    found = instances(ec2, Filters=[{"Name": "client-token", "Values": [execution_id]}])
    owned = [value for value in found if value.get("ClientToken") == execution_id]
    if not owned:
        # 作成の成否不明と「存在しない」の確定を区別する。
        return {"InstanceId": "", "State": "NoConfirmedOwnedInstance"}
    if len(owned) != 1:
        raise LifecycleError("CleanupOwnershipAmbiguous")
    value = owned[0]
    instance_id = value["InstanceId"]
    if value["State"]["Name"] == "terminated":
        return {"InstanceId": instance_id, "State": "AlreadyTerminated"}
    try:
        validate_instance(value, config)
        return destroy(ec2, ssm, config, instance_id, **wait_options)
    except Exception:
        # 後続のfailedステップで失敗を維持し、片付け対象IDは履歴へ残す。
        return {"InstanceId": instance_id, "State": "CleanupFailedInspectInstance"}


def handler(events, context):
    import boto3
    from botocore.config import Config

    config = events["config"]
    options = Config(
        retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=10
    )
    ec2 = boto3.client("ec2", region_name=config["region"], config=options)
    ssm = boto3.client("ssm", region_name=config["region"], config=options)
    try:
        if events["action"] == "create":
            return create(ec2, config, events["execution_id"])
        if events["action"] == "cleanup":
            return cleanup(ec2, ssm, config, events["execution_id"])
        if events["action"] == "destroy":
            return destroy(ec2, ssm, config, events["instance_id"])
        raise LifecycleError("UnsupportedAction")
    except LifecycleError:
        raise
    except Exception as error:
        code = getattr(error, "response", {}).get("Error", {}).get("Code", "Unknown")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code):
            code = "Unknown"
        raise LifecycleError("AwsOperationFailed_" + code) from None
