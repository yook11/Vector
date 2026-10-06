import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import botocore.session
from botocore.validate import validate_parameters

sys.path.insert(0, str(Path(__file__).parents[1]))
from sqs_operations import bastion
from sqs_operations.aws_cli import AwsFailure

EXECUTION = "00000000-0000-4000-8000-000000000001"
IID = "i-00000000000000001"


class BastionTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.config = bastion.Configuration()

    def tearDown(self):
        response = self.client.request.return_value
        if isinstance(response, dict):
            for call in self.client.request.call_args_list:
                service, operation = call.args[:2]
                name = "".join(word.capitalize() for word in operation.split("-"))
                model = botocore.session.get_session().get_service_model(service)
                validate_parameters(response, model.operation_model(name).output_shape)

    def test_start_pins_numeric_version(self):
        self.client.request.return_value = {"AutomationExecutionId": EXECUTION}
        with (
            patch.object(bastion, "document", return_value=("3", {})),
            patch.object(
                bastion, "execution_status", return_value={"status": "Success"}
            ),
        ):
            bastion.start(self.client, self.config, "create", emit=lambda value: None)
        kwargs = self.client.request.call_args.kwargs
        self.assertEqual(kwargs["document_version"], "3")
        self.assertNotIn("parameters", kwargs)
        self.assertEqual(kwargs["mode"], "Auto")

    def test_unknown_start_never_retries(self):
        self.client.request.side_effect = AwsFailure("Timeout")
        with patch.object(bastion, "document", return_value=("3", {})):
            with self.assertRaisesRegex(RuntimeError, "未確定"):
                bastion.start(
                    self.client, self.config, "create", emit=lambda value: None
                )
        self.client.request.assert_called_once()

    def test_wait_timeout_keeps_execution(self):
        self.client.request.return_value = {"AutomationExecutionId": EXECUTION}
        with patch.object(bastion, "document", return_value=("3", {})):
            result = bastion.start(
                self.client,
                self.config,
                "create",
                clock=Mock(side_effect=[0, 800]),
                emit=lambda value: None,
            )
        self.assertEqual(result["execution_id"], EXECUTION)
        self.assertEqual(result["status"], "WaitingDeadlineReached")
        self.client.request.assert_called_once()

    def test_other_document_execution_is_rejected(self):
        self.client.request.return_value = {
            "AutomationExecution": {"DocumentName": "Other"}
        }
        with self.assertRaisesRegex(RuntimeError, "対象外"):
            bastion.execution_status(self.client, self.config, EXECUTION)

    def test_execution_does_not_expose_raw_failure(self):
        self.client.request.return_value = {
            "AutomationExecution": {
                "DocumentName": "vector-bastion-create",
                "DocumentVersion": "1",
                "AutomationExecutionStatus": "Failed",
                "FailureMessage": "SECRET",
                "Parameters": {"secret": ["SECRET"]},
                "StepExecutions": [
                    {
                        "StepName": "create",
                        "StepStatus": "Failed",
                        "FailureMessage": "SECRET",
                        "Outputs": {"InstanceId": [IID], "secret": ["SECRET"]},
                    }
                ],
            }
        }
        result = bastion.execution_status(self.client, self.config, EXECUTION)
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(result["instance_ids"], [IID])

    def test_failed_destroy_retains_explicit_instance_id(self):
        self.client.request.return_value = {
            "AutomationExecution": {
                "DocumentName": "vector-bastion-destroy",
                "DocumentVersion": "1",
                "AutomationExecutionStatus": "Failed",
                "Parameters": {"InstanceId": [IID], "other": ["SECRET"]},
                "StepExecutions": [],
            }
        }
        result = bastion.execution_status(self.client, self.config, EXECUTION)
        self.assertEqual(result["instance_ids"], [IID])
        self.assertNotIn("SECRET", json.dumps(result))

    def test_wrong_account_is_rejected_before_operations(self):
        self.client.request.return_value = {
            "Account": "733360597472",
            "UserId": "test-user-id",
            "Arn": "arn:aws:sts::733360597472:assumed-role/vector-operations/test",
        }
        with self.assertRaises(RuntimeError):
            bastion.verify_identity(self.client)
        self.client.request.assert_called_once_with("sts", "get-caller-identity")

    def test_destroy_requires_instance_id(self):
        with patch.object(bastion, "document", return_value=("3", {})):
            with self.assertRaises(ValueError):
                bastion.start(self.client, self.config, "destroy")
        self.client.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
