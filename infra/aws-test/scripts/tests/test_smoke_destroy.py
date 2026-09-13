"""AWSへ接続せず、削除の境界・再実行・証拠の保持を検証する。"""

import copy
import fcntl
import hashlib
import io
import json
import subprocess
import sys
import unittest
from contextlib import nullcontext, redirect_stdout
from unittest.mock import Mock, patch

import test_smoke_up as up_tests
from smoke_runner import cleanup, common, controller


class DestroyTests(unittest.TestCase):
    def setUp(self):
        fixture = up_tests.RunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.run = fixture.run
        self.directory = fixture.directory
        self.run.backend["bucket"] = "vector-test-tfstate-123456789012"
        common.save(self.directory / "backend.json", self.run.backend)
        self.run.result.update(
            apply_attempted=True,
            state=self.run.backend["bucket"] + "/" + self.run.backend["key"],
            tests=[{"name": "previous", "status": "failed"}],
        )
        self.run.authenticate = Mock()
        self.run.claim = Mock()
        self.run.initialize = Mock()
        self.run.destroy = controller.Run.destroy.__get__(self.run)
        self.managed = True
        self.apply_error = None
        self.commands = []
        self.state = {
            "values": {
                "root_module": {
                    "resources": [
                        {"type": "aws_vpc", "values": {"id": "vpc-old"}},
                        {
                            "type": "aws_lambda_event_source_mapping",
                            "values": {"uuid": "mapping-old"},
                        },
                        {
                            "type": "aws_db_instance",
                            "values": {
                                "master_user_secret": [{"secret_arn": "secret-old"}]
                            },
                        },
                    ]
                }
            }
        }

        def tf(args, log, timeout=300):
            self.commands.append(args)
            destination = self.run.destroy_directory / log
            if args == ["show", "-json"]:
                common.save(destination, self.state if self.managed else {})
            elif args == ["state", "list"]:
                destination.write_text("aws_vpc.test\n" if self.managed else "")
            else:
                destination.write_text("terraform output\n")
                if args[0] == "plan":
                    (self.run.destroy_directory / "destroy.tfplan").write_bytes(b"plan")
                if args[0] == "apply":
                    if self.apply_error:
                        raise self.apply_error
                    self.managed = False

        self.run.tf = Mock(side_effect=tf)
        self.log_error = None
        self.log_calls = 0

        def collect(*, destination):
            self.log_calls += 1
            with self.run.phase("collection"):
                destination.mkdir()
                common.save(
                    destination / "log-collection.json",
                    {"runner": {"status": "collected"}},
                )
                if self.log_error:
                    raise self.log_error

        self.run.collect = Mock(side_effect=collect)
        self.inventory = patch.object(cleanup, "inventory", return_value={}).start()

    def destroy(self, **kwargs):
        with redirect_stdout(io.StringIO()):
            self.run.destroy(**kwargs)

    def latest(self):
        return self.run.result["destroy_attempts"][-1]

    def test_success_and_deleted_retry_preserve_ids_tests_and_history(self):
        sentinel = self.directory / "test-attempts/1/code/test.py"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("test code")
        before = copy.deepcopy(self.run.result["tests"])
        self.destroy()
        first = self.directory / "destroy-attempts/1"
        saved = (first / "result.json").read_bytes()
        self.assertTrue((first / "destroy.tfplan").exists())
        self.assertEqual(self.latest()["deletion_status"], "passed")
        self.destroy()
        self.assertEqual(sum(args[0] == "apply" for args in self.commands), 1)
        self.assertEqual((first / "result.json").read_bytes(), saved)
        self.assertEqual(sentinel.read_text(), "test code")
        self.assertEqual(self.run.result["tests"], before)
        known = json.loads((self.directory / "inventory.json").read_text())
        self.assertEqual(known["Vpcs"], [{"VpcId": "vpc-old"}])
        self.assertEqual(known["Secrets"], ["secret-old"])
        self.assertEqual(known["EventSourceMappings"], ["mapping-old"])
        self.assertEqual(self.latest()["status"], "passed")
        self.assertEqual(len(self.run.result["destroy_attempts"]), 2)

    def test_log_failure_still_deletes_and_retry_does_not_inherit_failure(self):
        original = self.run.collect
        self.run.collect = controller.Run.collect.__get__(self.run)
        with (
            patch.object(
                controller,
                "identity",
                side_effect=RuntimeError("runner_identity_mismatch"),
            ),
            self.assertRaisesRegex(
                RuntimeError, "deletion_confirmed_log_collection_incomplete"
            ),
        ):
            self.destroy()
        self.assertFalse(self.managed)
        self.assertEqual(self.latest()["status"], "failed")
        self.assertEqual(self.latest()["deletion_status"], "passed")
        self.assertEqual(self.latest()["phases"]["collection"]["status"], "failed")
        self.run.collect = original
        self.destroy()
        self.assertEqual(self.latest()["status"], "passed")
        self.assertEqual(self.run.result["destroy_attempts"][0]["status"], "failed")

    def test_partial_apply_interrupt_and_timeout_leave_retriable_records(self):
        for error in (
            KeyboardInterrupt(),
            subprocess.TimeoutExpired("terraform", 5400),
        ):
            self.apply_error = error
            with (
                self.subTest(error=type(error).__name__),
                self.assertRaises(type(error)),
            ):
                self.destroy()
            self.assertTrue(self.managed)
            self.assertEqual(self.latest()["deletion_status"], "unconfirmed")
            self.assertEqual(self.latest()["phases"]["destroy"]["status"], "failed")
            self.assertTrue((self.directory / "backend.json").exists())
        self.apply_error = None
        self.destroy()
        self.assertEqual(self.latest()["status"], "passed")
        self.assertEqual(len(self.run.result["destroy_attempts"]), 3)

    def test_invalid_scope_identity_owner_and_saved_inputs_block_deletion(self):
        for boundary in (self.run.authenticate, self.run.claim):
            boundary.side_effect = RuntimeError("scope_mismatch")
            with self.assertRaisesRegex(RuntimeError, "scope_mismatch"):
                self.destroy()
            boundary.side_effect = None
        original = self.run.backend["key"]
        self.run.backend["key"] = "permanent/terraform.tfstate"
        with self.assertRaisesRegex(RuntimeError, "saved_state_scope_mismatch"):
            self.destroy()
        self.run.backend["key"] = original
        manifest_path = self.directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"]["backend.json"] = hashlib.sha256(
            (self.directory / "backend.json").read_bytes()
        ).hexdigest()
        common.save(manifest_path, manifest)
        common.save(self.directory / "backend.json", {"changed": True})
        with self.assertRaisesRegex(ValueError, "saved_inputs_changed"):
            self.destroy()
        self.assertEqual(self.commands, [])
        self.run.collect.assert_not_called()

    def test_unstarted_invalid_run_id_and_parallel_operation_are_rejected(self):
        self.run.result["apply_attempted"] = False
        phases = copy.deepcopy(self.run.result["phases"])
        with self.assertRaisesRegex(RuntimeError, "not_started_provisioning"):
            self.destroy()
        self.assertEqual(self.commands, [])
        self.assertEqual(self.run.result["phases"], phases)
        with (
            patch.object(controller, "LOCAL", self.directory.parent.parent),
            patch.object(controller, "Run") as run,
            redirect_stdout(io.StringIO()),
            patch("sys.stderr", new=io.StringIO()),
        ):
            with (
                patch.object(
                    sys, "argv", ["aws-smoke.py", "destroy", "--run-id", "../sample"]
                ),
                self.assertRaises(SystemExit),
            ):
                controller.main()
            with (self.directory / "operation.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with (
                    patch.object(
                        sys, "argv", ["aws-smoke.py", "destroy", "--run-id", "sample"]
                    ),
                    self.assertRaises(BlockingIOError),
                ):
                    controller.main()
            run.assert_not_called()

    def test_pre_delete_inventory_failure_still_deletes_without_claiming_success(self):
        self.inventory.side_effect = [RuntimeError("inventory_unconfirmed"), {}]
        with self.assertRaisesRegex(RuntimeError, "pre_destroy_inventory_unconfirmed"):
            self.destroy()
        self.assertFalse(self.managed)
        self.assertEqual(self.latest()["deletion_status"], "unconfirmed")
        self.assertEqual(
            json.loads((self.directory / "inventory.json").read_text())["Secrets"],
            ["secret-old"],
        )

    def test_empty_state_remaining_or_unreadable_is_not_deleted_directly(self):
        self.managed = False
        for error in (
            RuntimeError("resources_remain_after_destroy"),
            up_tests.ClientError({"Error": {"Code": "AccessDenied"}}, "Describe"),
        ):
            with (
                patch.object(cleanup, "verify_deleted", side_effect=error),
                self.assertRaises(type(error)),
            ):
                self.destroy()
            self.assertEqual(self.latest()["deletion_status"], "unconfirmed")
        self.assertFalse(any(args[0] in {"plan", "apply"} for args in self.commands))

    def test_nonempty_state_after_apply_never_confirms_deletion(self):
        original = self.run.tf.side_effect

        def keep_state(args, log, timeout=300):
            original(args, log, timeout)
            if log == "state-after-destroy.txt":
                (self.run.destroy_directory / log).write_text("aws_vpc.remaining")

        self.run.tf.side_effect = keep_state
        with patch.object(cleanup, "verify_deleted") as verify:
            with self.assertRaisesRegex(RuntimeError, "terraform_state_not_empty"):
                self.destroy()
            verify.assert_not_called()
        self.assertEqual(self.latest()["deletion_status"], "unconfirmed")

    def test_one_shot_collection_is_reused_even_when_it_failed(self):
        shared = {
            "status": "failed",
            "directory": "test-attempts/1/logs",
            "error_type": "RuntimeError",
        }
        with self.assertRaisesRegex(RuntimeError, "log_collection_incomplete"):
            self.destroy(collection=shared)
        self.run.collect.assert_not_called()
        self.assertFalse(self.managed)
        self.assertEqual(self.latest()["phases"]["collection"], shared)

    def test_one_shot_deletes_after_database_test_and_collection_failure(self):
        for stage in ("database", "test", "collection"):
            self.managed = True
            attempt = Mock()
            attempt.path = self.directory / "test-attempts" / stage
            attempt.path.mkdir(parents=True)
            attempt.target, attempt.timeout = "aws_tests/example.py", 300
            attempt.result = {"target": attempt.target}
            attempt.cases.return_value = []
            if stage == "test":
                attempt.execute.side_effect = RuntimeError("test_failed")
            self.log_error = (
                RuntimeError("logs_failed") if stage == "collection" else None
            )
            previous = self.log_calls
            with (
                patch.object(controller.testing, "TestAttempt", return_value=attempt),
                patch.object(controller.assets, "prepare_assets"),
                patch.object(
                    self.run,
                    "prepare_database",
                    side_effect=RuntimeError("db_failed")
                    if stage == "database"
                    else None,
                ),
                redirect_stdout(io.StringIO()),
                self.subTest(stage=stage),
                self.assertRaises(RuntimeError),
            ):
                self.run.run(attempt.target)
            self.assertFalse(self.managed)
            self.assertEqual(self.log_calls, previous + 1)
            self.assertEqual(self.latest()["deletion_status"], "passed")
            self.assertEqual(
                self.latest()["phases"]["collection"]["directory"],
                f"test-attempts/{stage}/logs",
            )


class CleanupTests(unittest.TestCase):
    def test_missing_log_groups_are_distinct_from_denied_and_timeout(self):
        import tempfile
        from pathlib import Path

        for code in ("ResourceNotFoundException", "AccessDeniedException", None):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                api = Mock()
                api.get_paginator.return_value.paginate.side_effect = (
                    up_tests.ClientError({"Error": {"Code": code}}, "FilterLogEvents")
                    if code
                    else TimeoutError("deadline")
                )
                with patch.object(cleanup, "client", return_value=nullcontext(api)):
                    if code == "ResourceNotFoundException":
                        cleanup.collect_logs(
                            None,
                            {"execution": {"log_groups": {"runner": "group"}}},
                            Path(temporary),
                        )
                    else:
                        with self.assertRaisesRegex(
                            RuntimeError, "log_collection_incomplete"
                        ):
                            cleanup.collect_logs(
                                None,
                                {"execution": {"log_groups": {"runner": "group"}}},
                                Path(temporary),
                            )
                result = json.loads(
                    (Path(temporary) / "log-collection.json").read_text()
                )
                self.assertEqual(
                    result["runner"]["status"],
                    "absent" if code == "ResourceNotFoundException" else "unconfirmed",
                )

    def test_verification_waits_for_remaining_and_propagates_read_failure(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
            directory = Path(temporary)
            with (
                patch.object(cleanup, "inventory", side_effect=[{"Vpcs": ["vpc"]}, {}]),
                patch.object(cleanup.time, "sleep"),
            ):
                cleanup.verify_deleted(None, "sample", {}, directory)
            self.assertEqual(json.loads((directory / "remaining.json").read_text()), {})
            with (
                patch.object(cleanup, "inventory", return_value={"Vpcs": ["vpc"]}),
                patch.object(cleanup.time, "monotonic", side_effect=[0, 180, 180]),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "resources_remain_after_destroy"
                ):
                    cleanup.verify_deleted(None, "sample", {}, directory)
            with patch.object(
                cleanup,
                "inventory",
                side_effect=up_tests.ClientError(
                    {"Error": {"Code": "AccessDenied"}}, "Describe"
                ),
            ):
                with self.assertRaises(up_tests.ClientError):
                    cleanup.verify_deleted(None, "sample", {}, directory)
