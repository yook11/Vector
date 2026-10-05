import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import probe


def fixture():
    prefix = "vector-test-dlq-unit"
    cases = {}
    for name in ("control", "current", "queue-called-via", "queue-via-service"):
        queue_name = f"{prefix}-{name}"
        cases[name] = {
            "role_name": queue_name,
            "role_arn": f"arn:aws:iam::{probe.ACCOUNT}:role/vector-test/dlq-redrive/{queue_name}",
            "source_url": f"https://sqs.{probe.REGION}.amazonaws.com/{probe.ACCOUNT}/{queue_name}",
            "source_arn": f"arn:aws:sqs:{probe.REGION}:{probe.ACCOUNT}:{queue_name}",
            "dlq_url": f"https://sqs.{probe.REGION}.amazonaws.com/{probe.ACCOUNT}/{queue_name}-dlq",
            "dlq_arn": f"arn:aws:sqs:{probe.REGION}:{probe.ACCOUNT}:{queue_name}-dlq",
        }
    return {
        "account_id": probe.ACCOUNT,
        "phase": "seed",
        "prefix": prefix,
        "outside_url": f"https://sqs.{probe.REGION}.amazonaws.com/{probe.ACCOUNT}/{prefix}-outside",
        "cases": cases,
    }


class ProbeTests(unittest.TestCase):
    def test_iam_global_endpoint_uses_us_east_1_signing_region(self):
        result = probe.subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        with patch.object(probe.subprocess, "run", return_value=result) as run:
            probe.aws(
                "test",
                "iam",
                "get-role-policy",
                role_name="test-role",
                policy_name="test-policy",
            )
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--region") + 1], "us-east-1")
        self.assertEqual(
            command[command.index("--endpoint-url") + 1], "https://iam.amazonaws.com"
        )

    def test_sqs_uses_tokyo_endpoint_and_signing_region(self):
        result = probe.subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        with patch.object(probe.subprocess, "run", return_value=result) as run:
            probe.aws("test", "sqs", "get-queue-attributes", queue_url="test-url")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--region") + 1], "ap-northeast-1")
        self.assertEqual(
            command[command.index("--endpoint-url") + 1],
            "https://sqs.ap-northeast-1.amazonaws.com",
        )

    def test_network_failure_is_not_permission_denial(self):
        with patch.object(
            probe, "aws", side_effect=probe.AwsFailure("Could not connect")
        ):
            with self.assertRaises(probe.AwsFailure):
                probe.attempt("test", {}, "send-message")

    def test_foreign_account_stops_before_queue_mutation(self):
        data = fixture()
        with (
            patch("sys.argv", ["probe.py", "seed"]),
            patch.object(
                probe.subprocess, "check_output", return_value=json.dumps(data)
            ),
            patch.object(probe, "aws", return_value={"Account": "222753227567"}),
            patch.object(probe, "seed") as seed,
        ):
            with self.assertRaisesRegex(RuntimeError, "テスト専用アカウント"):
                probe.main()
            seed.assert_not_called()

    def test_foreign_queue_arn_stops_before_queue_mutation(self):
        data = fixture()
        data["cases"]["control"]["dlq_arn"] = (
            "arn:aws:sqs:ap-northeast-1:222753227567:production-dlq"
        )
        with (
            patch("sys.argv", ["probe.py", "seed"]),
            patch.object(
                probe.subprocess, "check_output", return_value=json.dumps(data)
            ),
            patch.object(probe, "aws", return_value={"Account": probe.ACCOUNT}),
            patch.object(probe, "seed") as seed,
        ):
            with self.assertRaisesRegex(RuntimeError, "ARN"):
                probe.main()
            seed.assert_not_called()

    def test_verify_requires_applied_verify_phase(self):
        with (
            patch("sys.argv", ["probe.py", "verify"]),
            patch.object(
                probe.subprocess, "check_output", return_value=json.dumps(fixture())
            ),
            patch.object(probe, "aws", return_value={"Account": probe.ACCOUNT}),
            patch.object(probe, "verify") as verify,
        ):
            with self.assertRaisesRegex(RuntimeError, "phase"):
                probe.main()
            verify.assert_not_called()

    def test_completed_task_with_stale_count_is_verified_and_credentials_not_saved(
        self,
    ):
        data = fixture()
        credentials = {
            "AccessKeyId": "test-key",
            "SecretAccessKey": "test-secret",
            "SessionToken": "test-token",
        }
        active = "control"
        started = set()

        def fake_aws(profile, service, operation, **kwargs):
            nonlocal active
            if operation == "assume-role":
                active = next(
                    name
                    for name, case in data["cases"].items()
                    if case["role_arn"] == kwargs["role_arn"]
                )
                return {"Credentials": copy.copy(credentials)}
            case = data["cases"][active]
            if operation == "get-caller-identity":
                return {
                    "Arn": f"arn:aws:sts::{probe.ACCOUNT}:assumed-role/{case['role_name']}/dlq-redrive-probe"
                }
            if operation == "get-queue-attributes":
                return {"Attributes": {}}
            if operation == "get-role-policy":
                return {"PolicyDocument": {}}
            if operation == "list-message-move-tasks":
                if active not in started:
                    return {}
                return {
                    "Results": [
                        {
                            "Status": "COMPLETED",
                            "SourceArn": case["dlq_arn"],
                            "DestinationArn": case["source_arn"],
                            "ApproximateNumberOfMessagesMoved": 0,
                        }
                    ]
                }
            if operation == "send-message":
                self.assertEqual(kwargs["credentials"], credentials)
                if kwargs["queue_url"] == data["outside_url"] or active != "control":
                    raise probe.AwsFailure(
                        "An error occurred (AccessDenied) when calling SendMessage"
                    )
                return {"MessageId": "direct"}
            if operation == "start-message-move-task":
                self.assertEqual(kwargs["credentials"], credentials)
                if active != "control":
                    raise probe.AwsFailure(
                        "An error occurred (AccessDenied) when calling StartMessageMoveTask"
                    )
                started.add(active)
                return {"TaskHandle": "handle"}
            if operation == "receive-message":
                return {
                    "Messages": [
                        {"Body": probe.marker(data, active), "ReceiptHandle": "receipt"}
                    ]
                }
            if operation == "delete-message":
                return {}
            self.fail(f"Unexpected API: {operation}")

        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "result.json"
            with (
                patch.object(probe, "aws", side_effect=fake_aws),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                probe.verify("test", data, report)
            result = json.loads(report.read_text())
            self.assertTrue(result["cases"]["control"]["payload_returned"])
            self.assertFalse(result["cases"]["current"]["redrive"]["allowed"])
            self.assertNotIn("test-secret", report.read_text())
            self.assertNotIn("test-token", report.read_text())


if __name__ == "__main__":
    unittest.main()
