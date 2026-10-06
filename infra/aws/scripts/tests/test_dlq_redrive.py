import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqs_operations import aws_cli, redrive, tunnel  # noqa: E402


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.pair = redrive.QueuePair.for_stage("assessment")
        self.client = Mock()
        self.tasks = []
        self.source = {
            "QueueArn": self.pair.source_arn,
            "SqsManagedSseEnabled": "true",
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": self.pair.dlq_arn}),
            **dict.fromkeys(redrive.COUNTS, "1"),
        }
        self.dlq = {
            "QueueArn": self.pair.dlq_arn,
            "SqsManagedSseEnabled": "true",
            **dict.fromkeys(redrive.COUNTS, "1"),
        }
        self.client.request.side_effect = self.respond

    def respond(self, service, operation, **kwargs):
        self.assertEqual(service, "sqs")
        self.assertEqual(kwargs["proxy_url"], "http://127.0.0.1:18444")
        if operation == "get-queue-attributes":
            return {
                "Attributes": self.source
                if kwargs["queue_url"] == self.pair.source_url
                else self.dlq
            }
        if operation == "list-message-move-tasks":
            return {"Results": self.tasks}
        if operation == "start-message-move-task":
            self.assertEqual(kwargs["source_arn"], self.pair.dlq_arn)
            self.assertEqual(kwargs["destination_arn"], self.pair.source_arn)
            self.assertEqual(kwargs["max_number_of_messages_per_second"], 1)
            return {"TaskHandle": "started"}
        if operation == "cancel-message-move-task":
            return {"ApproximateNumberOfMessagesMoved": 2}
        self.fail(operation)

    def operate(self, action, handle=None):
        return redrive.operate(
            self.client, "http://127.0.0.1:18444", self.pair, action, handle
        )

    def assert_not_mutated(self):
        self.assertFalse(
            any(
                call.args[1] in {"start-message-move-task", "cancel-message-move-task"}
                for call in self.client.request.call_args_list
            )
        )

    def test_status_reports_only_counts_and_allowlisted_task_fields(self):
        self.tasks = [
            {
                "Status": "FAILED",
                "FailureReason": "internal detail",
                "MessageBody": "secret",
            }
        ]
        result = self.operate("status")
        self.assertEqual(result["tasks"], [{"Status": "FAILED"}])
        self.assertEqual(result["counts"]["dlq"][redrive.COUNTS[0]], 1)
        self.assert_not_mutated()

    def test_start_returns_handle_and_uses_matching_destination_at_one_per_second(self):
        self.assertEqual(self.operate("start")["task_handle"], "started")

    def test_start_refuses_running_task(self):
        self.tasks = [{"Status": "RUNNING"}]
        with self.assertRaisesRegex(RuntimeError, "実行中"):
            self.operate("start")
        self.assert_not_mutated()

    def test_start_refuses_cancelling_task(self):
        self.tasks = [{"Status": "CANCELLING"}]
        with self.assertRaisesRegex(RuntimeError, "停止処理中"):
            self.operate("start")
        self.assert_not_mutated()

    def test_wrong_queue_arn_stops_before_mutation(self):
        self.dlq["QueueArn"] = "arn:aws:sqs:ap-northeast-1:111111111111:wrong"
        with self.assertRaisesRegex(RuntimeError, "ARN"):
            self.operate("start")
        self.assert_not_mutated()

    def test_wrong_redrive_pair_stops_before_mutation(self):
        self.source["RedrivePolicy"] = '{"deadLetterTargetArn":"other"}'
        with self.assertRaisesRegex(RuntimeError, "対応"):
            self.operate("start")
        self.assert_not_mutated()

    def test_kms_queue_stops_before_mutation(self):
        self.dlq["KmsMasterKeyId"] = "kms-key"
        with self.assertRaisesRegex(RuntimeError, "SSE-SQS"):
            self.operate("start")
        self.assert_not_mutated()

    def test_timeout_does_not_retry_start(self):
        def timeout(service, operation, **kwargs):
            if operation == "start-message-move-task":
                raise aws_cli.AwsFailure("Timeout")
            return self.respond(service, operation, **kwargs)

        self.client.request.side_effect = timeout
        with self.assertRaisesRegex(RuntimeError, "再実行せずstatus"):
            self.operate("start")
        self.assertEqual(
            sum(
                call.args[1] == "start-message-move-task"
                for call in self.client.request.call_args_list
            ),
            1,
        )

    def test_cancel_checks_running_handle_and_queue(self):
        self.tasks = [
            {
                "TaskHandle": "mine",
                "Status": "RUNNING",
                "SourceArn": self.pair.dlq_arn,
                "DestinationArn": self.pair.source_arn,
            }
        ]
        self.assertTrue(self.operate("cancel", "mine")["cancel_requested"])

    def test_cancel_refuses_other_handle(self):
        self.tasks = [
            {"TaskHandle": "other", "Status": "RUNNING", "SourceArn": self.pair.dlq_arn}
        ]
        with self.assertRaisesRegex(RuntimeError, "実行中タスク"):
            self.operate("cancel", "mine")
        self.assert_not_mutated()

    def test_cancel_refuses_other_source(self):
        self.tasks = [{"TaskHandle": "mine", "Status": "RUNNING", "SourceArn": "other"}]
        with self.assertRaisesRegex(RuntimeError, "実行中タスク"):
            self.operate("cancel", "mine")
        self.assert_not_mutated()

    def test_cancel_refuses_completed_task(self):
        self.tasks = [
            {
                "TaskHandle": "mine",
                "Status": "COMPLETED",
                "SourceArn": self.pair.dlq_arn,
            }
        ]
        with self.assertRaisesRegex(RuntimeError, "実行中タスク"):
            self.operate("cancel", "mine")
        self.assert_not_mutated()

    def test_unknown_stage_has_no_queue_mapping(self):
        with self.assertRaises(ValueError):
            redrive.QueuePair.for_stage("scheduler")

    def test_all_five_stage_destinations_are_fixed(self):
        names = {
            redrive.QueuePair.for_stage(stage).source_arn.rsplit(":", 1)[1]
            for stage in redrive.STAGES
        }
        self.assertEqual(
            names,
            {
                "vector-source-acquisition",
                "vector-article-completion",
                "vector-article-curation",
                "vector-article-assessment",
                "vector-article-embedding",
            },
        )


class CallerTests(unittest.TestCase):
    def test_wrong_account_is_rejected(self):
        client = Mock()
        client.request.return_value = {
            "Account": "other",
            "Arn": "arn:aws:sts::222753227567:assumed-role/vector-operations/operator",
        }
        with self.assertRaisesRegex(RuntimeError, "呼び出し元"):
            redrive.verify_identity(client)

    def test_other_role_with_similar_name_is_rejected(self):
        client = Mock()
        client.request.return_value = {
            "Account": redrive.ACCOUNT,
            "Arn": f"arn:aws:sts::{redrive.ACCOUNT}:assumed-role/vector-operations-admin/operator",
        }
        with self.assertRaisesRegex(RuntimeError, "呼び出し元"):
            redrive.verify_identity(client)

    def test_operations_role_is_accepted(self):
        client = Mock()
        client.request.return_value = {
            "Account": redrive.ACCOUNT,
            "Arn": f"arn:aws:sts::{redrive.ACCOUNT}:assumed-role/vector-operations/operator",
        }
        redrive.verify_identity(client)

    def test_cli_does_not_open_tunnel_for_wrong_caller(self):
        with (
            patch.object(
                sys,
                "argv",
                [
                    "dlq-redrive.py",
                    "start",
                    "--stage",
                    "assessment",
                    "--instance-id",
                    "i-0123456789abcdef0",
                ],
            ),
            patch.object(redrive, "profile_credentials", return_value={}),
            patch.object(redrive, "AwsCli") as client,
            patch.object(redrive, "sqs_tunnel") as connect,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            client.return_value.request.return_value = {
                "Account": "wrong",
                "Arn": "wrong",
            }
            self.assertEqual(redrive.main(), 1)
            connect.assert_not_called()

    def test_cli_rejects_arbitrary_destination_before_credentials(self):
        with (
            patch.object(
                sys,
                "argv",
                [
                    "dlq-redrive.py",
                    "start",
                    "--stage",
                    "assessment",
                    "--instance-id",
                    "i-0123456789abcdef0",
                    "--destination-arn",
                    "other",
                ],
            ),
            patch.object(redrive, "profile_credentials") as credentials,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit):
                redrive.main()
            credentials.assert_not_called()


class AwsCliTests(unittest.TestCase):
    def setUp(self):
        self.credentials = {
            "AccessKeyId": "test-key",
            "SecretAccessKey": "test-secret",
            "SessionToken": "test-token",
        }
        self.client = aws_cli.AwsCli(self.credentials)

    def test_proxy_and_credentials_cannot_be_overridden_by_environment(self):
        with patch.dict(
            aws_cli.os.environ,
            {
                "AWS_PROFILE": "admin",
                "AWS_ENDPOINT_URL": "https://other",
                "no_proxy": "*",
                "https_proxy": "http://other",
                "AWS_MAX_ATTEMPTS": "9",
            },
        ):
            env = self.client.environment("http://127.0.0.1:18444")
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:18444")
        self.assertEqual(env["NO_PROXY"], "")
        self.assertEqual(env["AWS_MAX_ATTEMPTS"], "1")
        self.assertNotIn("AWS_PROFILE", env)
        self.assertNotIn("AWS_ENDPOINT_URL", env)
        self.assertNotIn("https_proxy", env)
        self.assertNotIn("no_proxy", env)

    def test_sts_cannot_use_sqs_proxy(self):
        with patch.object(aws_cli, "run_json") as run:
            with self.assertRaises(ValueError):
                self.client.request(
                    "sts", "get-caller-identity", proxy_url="http://127.0.0.1:1234"
                )
            run.assert_not_called()

    def test_sqs_tls_endpoint_and_region_are_fixed_without_secret_arguments(self):
        command = self.client.command(
            "sqs", "start-message-move-task", source_arn="arn"
        )
        self.assertEqual(
            command[command.index("--endpoint-url") + 1], f"https://{aws_cli.HOST}"
        )
        self.assertEqual(command[command.index("--region") + 1], "ap-northeast-1")
        self.assertNotIn("--no-verify-ssl", command)
        self.assertNotIn("test-secret", " ".join(command))

    def test_cli_error_does_not_expose_raw_response(self):
        failure = subprocess.CompletedProcess(
            [], 1, "", "An error occurred (AccessDenied) secret-value body-value"
        )
        with patch.object(aws_cli.subprocess, "run", return_value=failure):
            with self.assertRaises(aws_cli.AwsFailure) as caught:
                self.client.request("sqs", "get-queue-attributes")
        self.assertEqual(caught.exception.code, "AccessDenied")
        self.assertNotIn("secret-value", str(caught.exception))
        self.assertNotIn("body-value", str(caught.exception))


class TunnelCleanupTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.process = Mock()
        self.process.pid = 1234
        self.process.poll.return_value = None
        self.process.stdout = io.StringIO(
            "Starting session with SessionId: operator-session\nWaiting for connections\n"
        )

    @contextlib.contextmanager
    def mocked_tunnel(self):
        with (
            patch.object(tunnel.subprocess, "Popen", return_value=self.process),
            patch.object(tunnel.os, "killpg") as kill,
            patch.object(
                tunnel,
                "connect_proxy",
                return_value=contextlib.nullcontext("http://127.0.0.1:18444"),
            ),
        ):
            yield kill

    def test_success_terminates_child_and_own_session(self):
        with self.mocked_tunnel() as kill:
            with tunnel.sqs_tunnel(
                self.client, "i-0123456789abcdef0", "vector-sqs-tunnel"
            ) as proxy:
                self.assertEqual(proxy, "http://127.0.0.1:18444")
        kill.assert_called_once_with(1234, tunnel.signal.SIGTERM)
        self.client.request.assert_called_once_with(
            "ssm", "terminate-session", session_id="operator-session"
        )

    def test_interruption_still_terminates_session_without_cancelling_sqs(self):
        with self.mocked_tunnel(), self.assertRaises(KeyboardInterrupt):
            with tunnel.sqs_tunnel(
                self.client, "i-0123456789abcdef0", "vector-sqs-tunnel"
            ):
                raise KeyboardInterrupt
        self.client.request.assert_called_once_with(
            "ssm", "terminate-session", session_id="operator-session"
        )

    def test_failed_operation_still_terminates_session(self):
        with (
            self.mocked_tunnel(),
            self.assertRaisesRegex(RuntimeError, "operation failed"),
        ):
            with tunnel.sqs_tunnel(
                self.client, "i-0123456789abcdef0", "vector-sqs-tunnel"
            ):
                raise RuntimeError("operation failed")
        self.client.request.assert_called_once_with(
            "ssm", "terminate-session", session_id="operator-session"
        )

    def test_cleanup_failure_is_reported_with_session_id(self):
        self.client.request.side_effect = aws_cli.AwsFailure("ExpiredToken")
        output = io.StringIO()
        with (
            self.mocked_tunnel(),
            contextlib.redirect_stderr(output),
            self.assertRaisesRegex(RuntimeError, "終了を確認"),
        ):
            with tunnel.sqs_tunnel(
                self.client, "i-0123456789abcdef0", "vector-sqs-tunnel"
            ):
                pass
        self.assertIn("operator-session", output.getvalue())
        self.assertNotIn("test-token", output.getvalue())


if __name__ == "__main__":
    unittest.main()
