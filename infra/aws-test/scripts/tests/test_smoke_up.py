"""AWSへ接続せず、起動確認の境界・再実行・失敗後の保持を検証する。"""

import fcntl
import io
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, nullcontext, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from smoke_runner import common, controller, readiness, snapshot  # noqa: E402


class RunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "runs" / "sample"
        self.directory.mkdir(parents=True)
        self.inputs = {
            "expected_account_id": "123456789012",
            "source_revision": "a" * 40,
            "backend_image_digest": "sha256:" + "b" * 64,
            "proxy_image_digest": "sha256:" + "c" * 64,
        }
        for name, value in {
            "manifest.json": {
                "run_id": "sample",
                "runner_profile": "runner",
                "owner_id": "owner",
                "files": {},
            },
            "inputs.tfvars.json": self.inputs,
            "backend.json": {
                "bucket": "test-state",
                "key": "smoke/sample/terraform.tfstate",
            },
        }.items():
            common.save(self.directory / name, value)
        self.addCleanup(patch.stopall)
        patch.object(controller, "session", return_value=object()).start()
        patch.object(
            controller, "client", side_effect=AssertionError("unexpected AWS call")
        ).start()
        patch.object(
            controller.assets,
            "prepare_assets",
            side_effect=AssertionError("unexpected auth schema"),
        ).start()
        patch.object(
            controller.remote,
            "prepare",
            side_effect=AssertionError("unexpected database"),
        ).start()
        patch.object(
            controller.remote,
            "ensure_image",
            side_effect=AssertionError("unexpected image pull"),
        ).start()
        patch.object(
            controller, "execute", side_effect=AssertionError("unexpected command")
        ).start()
        self.run = controller.Run(self.directory)
        self.run.preflight = Mock()
        self.run.provision = Mock(side_effect=self.provisioned)
        self.run.verify_resources = Mock(return_value={"output": True})
        self.run.collect = Mock()
        self.run.destroy = Mock()
        self.check = patch.object(readiness, "check").start()

    def provisioned(self):
        self.run.result.update(apply_attempted=True, apply_completed=True)
        self.run.result["phases"]["provision"] = {"status": "passed"}

    def up(self):
        with redirect_stdout(io.StringIO()):
            self.run.up()

    def test_up_only_provisions_and_checks(self):
        self.up()
        self.run.preflight.assert_called_once_with(resume=False)
        self.run.provision.assert_called_once()
        self.check.assert_called_once()
        self.run.collect.assert_not_called()
        self.run.destroy.assert_not_called()
        self.assertEqual(self.run.result["phases"]["up"]["status"], "passed")
        for name in ("auth_schema", "database", "test", "destroy"):
            self.assertEqual(self.run.result["phases"][name]["status"], "not_run")

    def test_recheck_does_not_apply_and_preserves_attempts(self):
        self.up()
        original = (self.directory / "up-attempts/1/result.json").read_bytes()
        self.check.side_effect = RuntimeError("ssm_failed")
        with self.assertRaisesRegex(RuntimeError, "ssm_failed"):
            self.up()
        self.run.preflight.assert_called_with(resume=True)
        self.assertEqual(self.run.provision.call_count, 1)
        self.assertEqual(
            (self.directory / "up-attempts/1/result.json").read_bytes(), original
        )
        self.assertEqual(self.run.result["phases"]["up"]["status"], "failed")
        self.assertEqual(self.run.result["phases"]["proxy_ecr"]["status"], "not_run")
        self.run.destroy.assert_not_called()

    def test_output_failure_after_apply_can_be_rechecked(self):
        self.run.verify_resources.side_effect = RuntimeError("output_failed")
        with self.assertRaisesRegex(RuntimeError, "output_failed"):
            self.up()
        self.run.verify_resources.side_effect = None
        self.up()
        self.assertEqual(self.run.provision.call_count, 1)

    def test_legacy_completed_report_can_be_rechecked(self):
        self.run.result["apply_attempted"] = True
        self.run.result["phases"]["provision"]["status"] = "passed"
        self.up()
        self.run.provision.assert_not_called()

    def test_partial_apply_and_deleted_runs_are_rejected(self):
        for name in ("partial", "cleanup_access", "destroy", "verification"):
            with self.subTest(name=name):
                self.run.result["apply_attempted"] = True
                if name != "partial":
                    self.run.result["phases"][name] = {"status": "failed"}
                with self.assertRaises(RuntimeError):
                    self.up()
                self.run.preflight.assert_not_called()
                self.run.provision.assert_not_called()
                self.run.destroy.assert_not_called()
                if name != "partial":
                    self.run.result["phases"][name] = {"status": "not_run"}

    def test_preflight_failure_can_retry_without_cleanup(self):
        self.run.preflight.side_effect = RuntimeError("access_failed")
        with self.assertRaises(RuntimeError):
            self.up()
        self.run.collect.assert_not_called()
        self.run.preflight.side_effect = None
        self.up()
        self.run.provision.assert_called_once()

    def test_interruption_keeps_environment_and_original_error(self):
        self.check.side_effect = KeyboardInterrupt()
        self.run.collect.side_effect = RuntimeError("logs_failed")
        with self.assertRaises(KeyboardInterrupt):
            self.up()
        self.run.collect.assert_called_once()
        self.run.destroy.assert_not_called()
        self.assertEqual(
            self.run.result["phases"]["up"]["error_type"], "KeyboardInterrupt"
        )

    def test_provision_records_completion_before_readback(self):
        common.save(
            self.directory / "create-plan.json",
            {
                "resource_changes": [
                    {"mode": "managed", "change": {"actions": ["create"]}}
                ],
            },
        )
        self.run.tf = Mock()
        controller.Run.provision(self.run)
        result = snapshot.load(self.directory / "result.json")
        self.assertTrue(result["apply_completed"])
        self.run.verify_resources.assert_not_called()

    def test_plan_with_update_is_rejected_before_apply(self):
        common.save(
            self.directory / "create-plan.json",
            {
                "resource_changes": [
                    {"mode": "managed", "change": {"actions": ["update"]}}
                ],
            },
        )
        self.run.tf = Mock()
        with self.assertRaisesRegex(RuntimeError, "existing_resources"):
            controller.Run.provision(self.run)
        self.assertFalse(self.run.result["apply_attempted"])
        self.assertFalse(
            any(c.args[0][0] == "apply" for c in self.run.tf.call_args_list)
        )

    def test_claim_retries_only_with_same_owner(self):
        api = Mock()
        api.put_object.side_effect = ClientError(
            {"Error": {"Code": "PreconditionFailed"}}, "PutObject"
        )
        for owner in (b"owner", b"someone-else"):
            with (
                self.subTest(owner=owner),
                patch.object(controller, "client", return_value=nullcontext(api)),
            ):
                api.get_object.return_value = {"Body": io.BytesIO(owner)}
                if owner == b"owner":
                    self.run.claim(create=True)
                else:
                    with self.assertRaisesRegex(RuntimeError, "owner_mismatch"):
                        self.run.claim(create=True)

    def test_input_change_rejected_by_snapshot_verification(self):
        manifest = snapshot.load(self.directory / "manifest.json")
        manifest["files"] = {"inputs.tfvars.json": "incorrect-hash"}
        common.save(self.directory / "manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "saved_inputs_changed"):
            controller.Run(self.directory)

    def resource_fixture(self):
        resources = {
            "vpc": "vpc-test",
            "subnets": {"runner": "subnet-runner"},
            "route_tables": {"private": "rtb-test"},
            "internet_gateway": "igw-test",
            "security_groups": {"ssm": "sg-ssm"},
            "ssm_endpoint": "vpce-ssm",
            "ssmmessages_endpoint": "vpce-messages",
            "instances": {"runner": "i-runner", "proxy": "i-proxy"},
            "root_volumes": {"runner": "vol-runner", "proxy": "vol-proxy"},
            "rds": "vector-test-sample",
            "event_source_mapping": "mapping-id",
            "roles": {"runner": "runner-role"},
            "instance_profiles": {"runner": "runner-profile"},
            "log_groups": {"runner": "/test/runner"},
            "master_secret_arn": "secret-id",
        }
        outputs = {
            "run": {
                "account_id": self.inputs["expected_account_id"],
                "run_id": "sample",
                "region": "ap-northeast-1",
                "state_bucket": self.run.backend["bucket"],
                "state_key": self.run.backend["key"],
                **{
                    k: self.inputs[k]
                    for k in (
                        "source_revision",
                        "backend_image_digest",
                        "proxy_image_digest",
                    )
                },
                "backend_image": "registry/image@"
                + self.inputs["backend_image_digest"],
            },
            "resources": resources,
            "execution": {"lambda_name": "vector-test-sample-embedding"},
        }
        inventory = {
            "Vpcs": [{"VpcId": "vpc-test"}],
            "Subnets": [{"SubnetId": "subnet-runner"}],
            "RouteTables": [{"RouteTableId": "rtb-test"}],
            "InternetGateways": [{"InternetGatewayId": "igw-test"}],
            "SecurityGroups": [{"GroupId": "sg-ssm"}],
            "VpcEndpoints": [
                {"VpcEndpointId": i} for i in ("vpce-ssm", "vpce-messages")
            ],
            "Instances": [{"InstanceId": i} for i in ("i-proxy", "i-runner")],
            "Volumes": [{"VolumeId": i} for i in ("vol-proxy", "vol-runner")],
            "DBInstances": [{"DBInstanceIdentifier": "vector-test-sample"}],
            "EventSourceMappings": ["mapping-id"],
            "Roles": ["runner"],
            "Profiles": ["runner"],
            "Logs": ["/test/runner"],
            "Secrets": ["secret-id"],
            "Queue": ["vector-test-sample"],
        }
        state = {
            "values": {
                "root_module": {
                    "resources": [
                        {"type": "aws_vpc_endpoint", "values": {"id": "vpce-messages"}},
                        {
                            "type": "aws_lambda_event_source_mapping",
                            "values": {"uuid": "mapping-id"},
                        },
                    ]
                }
            }
        }
        common.save(self.directory / "state-after-create.json", state)
        self.run.outputs = Mock(return_value=outputs)
        self.run.tf = Mock()
        api = Mock()
        api.get_function.return_value = {
            "Code": {"ResolvedImageUri": outputs["run"]["backend_image"]}
        }
        patch.object(controller, "client", return_value=nullcontext(api)).start()
        read = patch.object(
            controller.cleanup, "inventory", return_value=inventory
        ).start()
        return outputs, inventory, read, api

    def test_resources_are_read_back_and_mapping_is_known_before_inventory(self):
        outputs, _, read, _ = self.resource_fixture()
        result = controller.Run.verify_resources(self.run)
        self.assertEqual(result, outputs)
        known = read.call_args.args[2]
        self.assertEqual(known["EventSourceMappings"], ["mapping-id"])
        self.assertIn({"VpcEndpointId": "vpce-messages"}, known["VpcEndpoints"])

    def test_missing_message_endpoint_and_changed_lambda_image_fail(self):
        _, inventory, _, api = self.resource_fixture()
        inventory["VpcEndpoints"] = [{"VpcEndpointId": "vpce-ssm"}]
        with self.assertRaisesRegex(
            RuntimeError, "runtime_resources_missing:VpcEndpoints"
        ):
            controller.Run.verify_resources(self.run)
        inventory["VpcEndpoints"].append({"VpcEndpointId": "vpce-messages"})
        api.get_function.return_value = {
            "Code": {"ResolvedImageUri": "different-image"}
        }
        with self.assertRaisesRegex(RuntimeError, "lambda_image_digest_mismatch"):
            controller.Run.verify_resources(self.run)

    def test_legacy_resource_outputs_need_no_message_endpoint(self):
        outputs, inventory, _, _ = self.resource_fixture()
        del outputs["resources"]["ssmmessages_endpoint"]
        inventory["VpcEndpoints"] = [{"VpcEndpointId": "vpce-ssm"}]
        controller.Run.verify_resources(self.run)

    def test_old_report_can_still_be_destroyed(self):
        self.run.result["apply_attempted"] = True
        self.run.authenticate = Mock()
        self.run.claim = Mock()
        self.run.initialize = Mock()
        self.run.tf = Mock()
        common.save(self.directory / "state-before-destroy.json", {})
        (self.directory / "state-after-destroy.txt").write_text("")
        with (
            patch.object(controller.cleanup, "inventory", return_value={}),
            patch.object(controller.cleanup, "verify_deleted") as verify,
        ):
            controller.Run.destroy(self.run)
        verify.assert_called_once()
        self.assertEqual(self.run.result["phases"]["verification"]["status"], "passed")

    def test_cli_creates_snapshot_then_calls_up(self):
        with (
            patch.object(controller, "LOCAL", self.directory.parent.parent),
            patch.object(sys, "argv", ["aws-smoke.py", "up", "--run-id", "new-run"]),
            patch.object(controller.snapshot, "create") as create,
            patch.object(controller, "Run") as run,
        ):
            folder = self.directory.parent / "new-run"
            run.return_value.up.side_effect = lambda: (
                folder / "summary.txt"
            ).write_text("passed")
            with redirect_stdout(io.StringIO()):
                controller.main()
            create.assert_called_once_with(folder, "new-run", "vector-test-runner")
            run.return_value.up.assert_called_once()
            run.return_value.run.assert_not_called()

    def test_destroy_collects_new_logs_after_up(self):
        self.run.result["up_attempts"] = [{"number": 1}]
        self.run.result["phases"]["collection"]["status"] = "passed"
        self.run.persist()
        with (
            patch.object(controller, "LOCAL", self.directory.parent.parent),
            patch.object(
                sys, "argv", ["aws-smoke.py", "destroy", "--run-id", "sample"]
            ),
            patch.object(controller, "Run", return_value=self.run),
            redirect_stdout(io.StringIO()),
        ):
            controller.main()
        self.run.collect.assert_called_once()
        self.run.destroy.assert_called_once()

    def test_cli_rejects_parallel_operation_before_loading_run(self):
        with (self.directory / "operation.lock").open("a") as locked:
            fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with (
                patch.object(controller, "LOCAL", self.directory.parent.parent),
                patch.object(sys, "argv", ["aws-smoke.py", "up", "--run-id", "sample"]),
                patch.object(controller, "Run") as run,
            ):
                with self.assertRaises(BlockingIOError), redirect_stdout(io.StringIO()):
                    controller.main()
                run.assert_not_called()

    def test_one_shot_keeps_schema_before_apply_and_cleanup_after_database(self):
        calls = []
        self.run.preflight.side_effect = lambda: calls.append("preflight")
        self.run.provision.side_effect = lambda: (
            calls.append("provision"),
            self.provisioned(),
        )
        self.check.side_effect = lambda *args: calls.append("readiness")
        self.run.collect.side_effect = lambda **kwargs: calls.append("collect")
        self.run.destroy.side_effect = lambda: calls.append("destroy")

        def database(*args):
            calls.append("database")
            raise RuntimeError("database_failure")

        attempt = Mock()
        attempt.path = self.directory / "test-attempts/1"
        attempt.target = "aws_tests/embedding/test_event_processing.py"
        attempt.result = {"status": "running", "target": attempt.target}
        attempt.collect.side_effect = lambda: calls.append("selection")
        attempt.cases.return_value = []
        with (
            patch.object(controller.testing, "TestAttempt", return_value=attempt),
            patch.object(
                controller.assets,
                "prepare_assets",
                side_effect=lambda *args: calls.append("schema"),
            ),
            patch.object(controller.remote, "prepare", side_effect=database),
            patch.object(
                controller.remote,
                "ensure_image",
                side_effect=lambda *args: calls.append("image"),
            ),
        ):
            with (
                self.assertRaisesRegex(RuntimeError, "database_failure"),
                redirect_stdout(io.StringIO()),
            ):
                self.run.run(attempt.target)
        self.assertEqual(
            calls,
            [
                "selection",
                "preflight",
                "schema",
                "provision",
                "readiness",
                "image",
                "database",
                "collect",
                "destroy",
            ],
        )


class Clock:
    value = 0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class CommandTests(unittest.TestCase):
    def test_readiness_command_disables_log_forwarding_and_caps_wait(self):
        api = Mock()
        api.send_command.return_value = {"Command": {"CommandId": "new-command"}}
        outputs = {
            "execution": {
                "instance_ids": {"runner": "i-runner"},
                "log_groups": {"runner": "/test/runner"},
            }
        }
        journal = Mock()
        with (
            patch.object(common, "client", return_value=nullcontext(api)),
            patch.object(common.time, "monotonic", return_value=100),
            patch.object(common, "wait_command", return_value="reply") as wait,
        ):
            result = common.send_command(
                object(),
                outputs,
                "echo reply",
                30,
                journal,
                cloudwatch=False,
                deadline=110,
            )
        self.assertEqual(result, "reply")
        self.assertFalse(
            api.send_command.call_args.kwargs["CloudWatchOutputConfig"][
                "CloudWatchOutputEnabled"
            ]
        )
        self.assertEqual(wait.call_args.args[3], 110)
        journal.assert_called_once_with("new-command")

    def test_expired_deadline_sends_nothing(self):
        with (
            patch.object(common.time, "monotonic", return_value=900),
            patch.object(common, "client") as client,
        ):
            with self.assertRaises(TimeoutError):
                common.send_command(
                    object(), {"execution": {}}, "echo reply", 30, Mock(), deadline=900
                )
        client.assert_not_called()

    def test_nonzero_response_is_not_success(self):
        api = Mock()
        api.exceptions.InvocationDoesNotExist = LookupError
        api.get_command_invocation.return_value = {
            "Status": "Success",
            "ResponseCode": 1,
        }
        with (
            patch.object(common, "client", return_value=nullcontext(api)),
            patch.object(common.time, "monotonic", return_value=0),
        ):
            with self.assertRaisesRegex(RuntimeError, "ssm_command_failed"):
                common.wait_command(object(), "i-runner", "command", 30)

    def test_execution_timeout_still_interrupts_subprocess(self):
        clock = Clock()
        process = Mock(pid=123)

        def wait(timeout):
            if clock.value:
                return 0
            clock.sleep(timeout)
            raise subprocess.TimeoutExpired(["terraform"], timeout)

        process.wait.side_effect = wait
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(common, "time", clock),
            patch.object(common.subprocess, "Popen", return_value=process),
            patch.object(common.os, "killpg") as kill,
        ):
            with (
                self.assertRaises(subprocess.TimeoutExpired),
                redirect_stdout(io.StringIO()),
            ):
                common.execute(
                    ["terraform"],
                    cwd=directory,
                    log=Path(directory) / "create.log",
                    timeout=1,
                    progress=True,
                )
        kill.assert_called_once_with(123, common.signal.SIGINT)


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        self.clock = Clock()
        patch.object(readiness, "time", self.clock).start()
        self.ids = ["i-proxy", "i-runner"]
        self.outputs = {
            "execution": {
                "instance_ids": dict(zip(("proxy", "runner"), self.ids, strict=True))
            },
            "run": {
                "account_id": "123456789012",
                "proxy_image_digest": "sha256:" + "c" * 64,
            },
        }
        self.api = Mock()
        self.api.describe_instance_status.return_value = {
            "InstanceStatuses": [
                {
                    "InstanceId": i,
                    "InstanceState": {"Name": "running"},
                    "InstanceStatus": {"Status": "ok"},
                    "SystemStatus": {"Status": "ok"},
                }
                for i in self.ids
            ]
        }
        self.api.describe_instance_information.return_value = {
            "InstanceInformationList": [
                {"InstanceId": i, "PingStatus": "Online"} for i in self.ids
            ]
        }
        patch.object(
            readiness, "client", side_effect=lambda *args: nullcontext(self.api)
        ).start()
        self.send = patch.object(
            readiness, "send_command", side_effect=self.respond
        ).start()
        self.overrides = {}
        self.states = {}
        self.journal = Mock()

    def respond(self, aws, outputs, cmd, timeout, journal, **kwargs):
        self.assertFalse(kwargs["cloudwatch"])
        self.assertEqual(kwargs["deadline"], 900)
        self.assertEqual(timeout, 30)
        inner = shlex.split(cmd)[2]
        subprocess.run(
            ["/bin/bash", "-n"],
            input=inner,
            text=True,
            check=True,
            capture_output=True,
        )
        journal("command-" + str(self.send.call_count))
        if "systemctl is-active" in inner:
            return self.overrides.get("bootstrap", '{"status":"ready"}\nactive')
        if "proxy_connected" in inner:
            return self.overrides.get("tcp", "proxy_connected")
        if "batch-get-image" in inner:
            return self.overrides.get(
                "ecr",
                json.dumps(
                    {
                        "images": [self.outputs["run"]["proxy_image_digest"]],
                        "failures": [],
                    }
                ),
            )
        return self.overrides.get("echo", shlex.split(inner)[-1])

    @contextmanager
    def phase(self, name):
        self.states[name] = "running"
        try:
            yield
        except BaseException:
            self.states[name] = "failed"
            raise
        else:
            self.states[name] = "passed"

    def check(self):
        with redirect_stdout(io.StringIO()):
            readiness.check(object(), object(), self.outputs, self.phase, self.journal)

    def test_all_checks_pass_and_command_ids_are_journaled(self):
        self.check()
        self.assertEqual(self.states, dict.fromkeys(readiness.PHASES, "passed"))
        self.assertEqual(self.journal.call_count, 4)

    def test_offline_ssm_times_out_without_sending_commands(self):
        self.api.describe_instance_information.return_value = {
            "InstanceInformationList": []
        }
        with self.assertRaisesRegex(TimeoutError, "readiness_timeout:ssm"):
            self.check()
        self.send.assert_not_called()
        self.assertEqual(self.clock.value, 900)

    def test_stopped_instance_fails_immediately(self):
        self.api.describe_instance_status.return_value["InstanceStatuses"][0][
            "InstanceState"
        ]["Name"] = "stopped"
        with self.assertRaisesRegex(RuntimeError, "instance_not_running"):
            self.check()
        self.assertEqual(self.clock.value, 0)

    def test_failed_bootstrap_never_waits_or_tests_proxy(self):
        self.overrides["bootstrap"] = '{"status":"failed"}\ninactive'
        with self.assertRaisesRegex(RuntimeError, "runner_bootstrap_failed"):
            self.check()
        self.assertNotIn("proxy_tcp", self.states)
        self.assertEqual(self.clock.value, 0)

    def test_starting_bootstrap_respects_shared_deadline(self):
        self.overrides["bootstrap"] = '{"status":"starting"}\ninactive'
        with self.assertRaisesRegex(TimeoutError, "readiness_timeout:bootstrap"):
            self.check()
        self.assertEqual(self.clock.value, 900)

    def test_bad_or_empty_responses_never_pass(self):
        for stage, result in (
            ("echo", "previous-call"),
            ("bootstrap", '{"status":"ready"}\ninactive'),
            ("bootstrap", ""),
            ("tcp", ""),
            ("ecr", '{"images":[],"failures":[]}'),
            ("ecr", '{"images":[],"failures":["ImageNotFound"]}'),
            ("ecr", '{"images":["wrong-digest"],"failures":[]}'),
        ):
            with self.subTest(stage=stage, result=result):
                self.overrides = {stage: result}
                with self.assertRaises((RuntimeError, ValueError)):
                    self.check()

    def test_delivery_failure_is_not_hidden(self):
        self.send.side_effect = RuntimeError("ssm_command_failed:DeliveryTimedOut")
        with self.assertRaisesRegex(RuntimeError, "DeliveryTimedOut"):
            self.check()
        self.assertEqual(self.states["ssm_command"], "failed")


if __name__ == "__main__":
    unittest.main()
