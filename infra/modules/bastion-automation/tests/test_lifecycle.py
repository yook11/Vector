"""AWS応答schemaと実行所有権に基づく踏み台の契約を検証する。"""

from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock

import botocore.session
from botocore.validate import validate_parameters
from botocore.exceptions import ClientError

SPEC = importlib.util.spec_from_file_location(
    "lifecycle", Path(__file__).parents[1] / "lifecycle.py"
)
lifecycle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lifecycle)
IID = "i-00000000000000001"
OTHER = "i-00000000000000002"
TOKEN = "00000000-0000-4000-8000-000000000001"
CONFIG = {
    "account_id": "123456789012",
    "region": "ap-northeast-1",
    "network": {
        "vpc_id": "vpc-00000000000000001",
        "subnet_id": "subnet-00000000000000001",
        "security_group_id": "sg-00000000000000001",
        "network_interface_id": "eni-00000000000000001",
    },
    "template_id": "lt-00000000000000001",
    "template_version": "1",
    "ami": "ami-00000000000000001",
    "profile_arn": "arn:aws:iam::123456789012:instance-profile/vector-bastion",
    "tags": {
        "Name": "vector-bastion",
        "vector:managed-bastion": "vector",
        "vector:session-purpose": "sqs-redrive",
    },
}
INSTANCE = {
    "InstanceId": IID,
    "VpcId": CONFIG["network"]["vpc_id"],
    "SubnetId": CONFIG["network"]["subnet_id"],
    "ImageId": CONFIG["ami"],
    "InstanceType": "t4g.nano",
    "ClientToken": TOKEN,
    "State": {"Name": "running", "Code": 16},
    "IamInstanceProfile": {"Arn": CONFIG["profile_arn"], "Id": "PROFILE"},
    "MetadataOptions": {"HttpTokens": "required"},
    "RootDeviceName": "/dev/xvda",
    "BlockDeviceMappings": [
        {
            "DeviceName": "/dev/xvda",
            "Ebs": {"VolumeId": "vol-00000000000000001", "DeleteOnTermination": True},
        }
    ],
    "Tags": [
        {"Key": k, "Value": v}
        for k, v in {
            **CONFIG["tags"],
            "aws:ec2launchtemplate:id": CONFIG["template_id"],
            "aws:ec2launchtemplate:version": "1",
        }.items()
    ],
    "NetworkInterfaces": [
        {
            "NetworkInterfaceId": CONFIG["network"]["network_interface_id"],
            "Attachment": {
                "InstanceId": IID,
                "DeviceIndex": 0,
                "DeleteOnTermination": False,
            },
        }
    ],
}
# InstanceNetworkInterfaceAttachmentにはInstanceIdが無いため、ENI応答と分ける。
INSTANCE["NetworkInterfaces"][0]["Attachment"].pop("InstanceId")
ENI = {
    "NetworkInterfaceId": CONFIG["network"]["network_interface_id"],
    "OwnerId": CONFIG["account_id"],
    "VpcId": CONFIG["network"]["vpc_id"],
    "SubnetId": CONFIG["network"]["subnet_id"],
    "Groups": [{"GroupId": CONFIG["network"]["security_group_id"]}],
    "Status": "in-use",
    "Attachment": {"InstanceId": IID, "DeviceIndex": 0, "DeleteOnTermination": False},
}


def reservation(instance):
    return {"Reservations": [{"Instances": [instance]}]}


class Clock:
    def __init__(self):
        self.value = 0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.ec2, self.ssm = Mock(), Mock()
        self.ec2.describe_network_interfaces.return_value = {
            "NetworkInterfaces": [deepcopy(ENI)]
        }
        self.ec2.describe_instances.return_value = reservation(deepcopy(INSTANCE))
        self.ssm.describe_sessions.return_value = {"Sessions": []}
        self.clock = Clock()
        self.wait = {"clock": self.clock.now, "sleep": self.clock.sleep}

    def test_fixtures_match_sdk_response_shapes(self):
        model = botocore.session.get_session().get_service_model("ec2")
        for operation, response in [
            ("DescribeInstances", reservation(INSTANCE)),
            ("DescribeNetworkInterfaces", {"NetworkInterfaces": [ENI]}),
        ]:
            validate_parameters(response, model.operation_model(operation).output_shape)

    def test_reuses_existing_without_launch(self):
        self.assertEqual(
            lifecycle.create(self.ec2, CONFIG, TOKEN),
            {"InstanceId": IID, "Reused": True},
        )
        self.ec2.run_instances.assert_not_called()

    def test_shutting_down_never_launches(self):
        self.ec2.describe_instances.return_value["Reservations"][0]["Instances"][0][
            "State"
        ]["Name"] = "shutting-down"
        with self.assertRaisesRegex(
            lifecycle.LifecycleError, "InstanceState_shutting-down"
        ):
            lifecycle.create(self.ec2, CONFIG, TOKEN)
        self.ec2.run_instances.assert_not_called()

    def test_concurrent_launch_reuses_winner(self):
        free = deepcopy(ENI)
        free.pop("Attachment")
        free["Status"] = "available"
        self.ec2.describe_network_interfaces.side_effect = [
            {"NetworkInterfaces": [free]},
            {"NetworkInterfaces": [ENI]},
        ]
        winner = deepcopy(INSTANCE)
        winner["ClientToken"] = "other-execution"
        self.ec2.describe_instances.return_value = reservation(winner)
        self.ec2.run_instances.side_effect = RuntimeError("Interface in use")
        result = lifecycle.create(self.ec2, CONFIG, TOKEN, **self.wait)
        self.assertTrue(result["Reused"])
        self.ec2.run_instances.assert_called_once_with(
            LaunchTemplate={"LaunchTemplateId": CONFIG["template_id"], "Version": "1"},
            MinCount=1,
            MaxCount=1,
            ClientToken=TOKEN,
        )

    def test_lost_response_resolves_own_instance(self):
        free = deepcopy(ENI)
        free.pop("Attachment")
        free["Status"] = "available"
        self.ec2.describe_network_interfaces.side_effect = [
            {"NetworkInterfaces": [free]},
            {"NetworkInterfaces": [ENI]},
        ]
        self.ec2.run_instances.side_effect = TimeoutError()
        self.assertFalse(
            lifecycle.create(self.ec2, CONFIG, TOKEN, **self.wait)["Reused"]
        )

    def test_instance_visibility_delay_does_not_trigger_cleanup(self):
        self.ec2.describe_instances.side_effect = [
            ClientError(
                {"Error": {"Code": "InvalidInstanceID.NotFound"}}, "DescribeInstances"
            ),
            reservation(INSTANCE),
        ]
        result = lifecycle.create(self.ec2, CONFIG, TOKEN, **self.wait)
        self.assertEqual(result["InstanceId"], IID)
        self.ec2.run_instances.assert_not_called()
        self.ec2.terminate_instances.assert_not_called()

    def test_unknown_launch_does_not_retry(self):
        free = deepcopy(ENI)
        free.pop("Attachment")
        free["Status"] = "available"
        self.ec2.describe_network_interfaces.return_value = {
            "NetworkInterfaces": [free]
        }
        self.ec2.run_instances.side_effect = TimeoutError()
        with self.assertRaisesRegex(lifecycle.LifecycleError, "LaunchOutcomeUnknown"):
            lifecycle.create(self.ec2, CONFIG, TOKEN, **self.wait)
        self.ec2.run_instances.assert_called_once()

    def test_capacity_rejection_is_definitive_and_does_not_retry(self):
        free = deepcopy(ENI)
        free.pop("Attachment")
        free["Status"] = "available"
        self.ec2.describe_network_interfaces.return_value = {
            "NetworkInterfaces": [free]
        }
        self.ec2.run_instances.side_effect = ClientError(
            {"Error": {"Code": "InsufficientInstanceCapacity"}}, "RunInstances"
        )
        with self.assertRaisesRegex(
            lifecycle.LifecycleError, "LaunchRejected_InsufficientInstanceCapacity"
        ):
            lifecycle.create(self.ec2, CONFIG, TOKEN, **self.wait)
        self.ec2.run_instances.assert_called_once()
        self.assertEqual(self.clock.value, 0)

    def test_wrong_instance_refused(self):
        with self.assertRaisesRegex(
            lifecycle.LifecycleError, "InstanceDoesNotOwnInterface"
        ):
            lifecycle.destroy(self.ec2, self.ssm, CONFIG, OTHER)
        self.ec2.terminate_instances.assert_not_called()

    def test_active_session_refused(self):
        self.ssm.describe_sessions.return_value = {
            "Sessions": [{"SessionId": "active"}]
        }
        with self.assertRaisesRegex(lifecycle.LifecycleError, "ActiveSessionRefused"):
            lifecycle.destroy(self.ec2, self.ssm, CONFIG, IID)
        self.ec2.terminate_instances.assert_not_called()

    def test_foreign_tags_refused(self):
        self.ec2.describe_instances.return_value["Reservations"][0]["Instances"][0][
            "Tags"
        ] = []
        with self.assertRaisesRegex(lifecycle.LifecycleError, "UnexpectedInstance"):
            lifecycle.destroy(self.ec2, self.ssm, CONFIG, IID)
        self.ec2.terminate_instances.assert_not_called()

    def test_destroy_never_follows_successor(self):
        terminated = deepcopy(INSTANCE)
        terminated["State"]["Name"] = "terminated"
        successor = deepcopy(ENI)
        successor["Attachment"]["InstanceId"] = OTHER
        self.ec2.describe_instances.side_effect = [
            reservation(INSTANCE),
            reservation(terminated),
        ]
        self.ec2.describe_network_interfaces.side_effect = [
            {"NetworkInterfaces": [ENI]},
            {"NetworkInterfaces": [successor]},
        ]
        self.ec2.describe_volumes.return_value = {"Volumes": []}
        self.assertEqual(
            lifecycle.destroy(self.ec2, self.ssm, CONFIG, IID, **self.wait)["State"],
            "terminated",
        )
        self.ec2.terminate_instances.assert_called_once_with(InstanceIds=[IID])

    def test_cleanup_never_deletes_reused_instance(self):
        winner = deepcopy(INSTANCE)
        winner["ClientToken"] = "someone-else"
        self.ec2.describe_instances.return_value = reservation(winner)
        self.assertEqual(
            lifecycle.cleanup(self.ec2, self.ssm, CONFIG, TOKEN)["State"],
            "NoConfirmedOwnedInstance",
        )
        self.ec2.terminate_instances.assert_not_called()

    def test_cleanup_failure_is_reported(self):
        self.ec2.terminate_instances.side_effect = RuntimeError("failed")
        result = lifecycle.cleanup(self.ec2, self.ssm, CONFIG, TOKEN, **self.wait)
        self.assertEqual(
            result, {"InstanceId": IID, "State": "CleanupFailedInspectInstance"}
        )

    def test_cleanup_preserves_id_when_configuration_changed(self):
        self.ec2.describe_instances.return_value["Reservations"][0]["Instances"][0][
            "ImageId"
        ] = "ami-00000000000000002"
        result = lifecycle.cleanup(self.ec2, self.ssm, CONFIG, TOKEN, **self.wait)
        self.assertEqual(result["InstanceId"], IID)
        self.assertEqual(result["State"], "CleanupFailedInspectInstance")
        self.ec2.terminate_instances.assert_not_called()

    def test_retained_root_refused(self):
        self.ec2.describe_instances.return_value["Reservations"][0]["Instances"][0][
            "BlockDeviceMappings"
        ][0]["Ebs"]["DeleteOnTermination"] = False
        with self.assertRaisesRegex(lifecycle.LifecycleError, "UnexpectedRootVolume"):
            lifecycle.destroy(self.ec2, self.ssm, CONFIG, IID)
        self.ec2.terminate_instances.assert_not_called()


if __name__ == "__main__":
    unittest.main()
