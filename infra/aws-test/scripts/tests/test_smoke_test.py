"""AWSを使わず、対象選択・結果保存・環境保持の重要な条件を検証する。"""

import copy
import fcntl
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_smoke_up as up_tests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from smoke_runner import controller, testing  # noqa: E402


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        root = testing.ROOT
        temporary = tempfile.TemporaryDirectory(
            dir=root / "infra/aws-test/scripts/tests"
        )
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "backend/aws_tests"
        (self.source / "embedding").mkdir(parents=True)
        (self.root / "backend/.venv").symlink_to(
            root / "backend/.venv", target_is_directory=True
        )
        for name in (
            "__init__.py",
            "reporting.py",
            "conftest.py",
            "embedding/__init__.py",
            "embedding/runtime.py",
        ):
            shutil.copyfile(root / "backend/aws_tests" / name, self.source / name)
        self.run = self.root / "runs/sample"
        self.run.mkdir(parents=True)
        self.file = self.source / "test_cases.py"
        self.file.write_text("def test_first(): pass\ndef test_second(): pass\n")
        self.addCleanup(patch.stopall)
        patch.object(testing, "ROOT", self.root).start()

    def attempt(self, target="aws_tests/test_cases.py", timeout=10):
        attempt = testing.TestAttempt(self.run, target, timeout)
        attempt.collect()
        return attempt

    def test_directory_file_and_node_select_only_requested_cases(self):
        for target, count in (
            ("aws_tests/", 2),
            ("aws_tests/test_cases.py", 2),
            ("aws_tests/test_cases.py::test_second", 1),
        ):
            with self.subTest(target=target):
                attempt = self.attempt(target)
                attempt.execute("unused")
                self.assertEqual(len(attempt.cases()), count)
                self.assertTrue(
                    all(case["status"] == "passed" for case in attempt.cases())
                )
                self.assertEqual(attempt.result["execution"]["exit_code"], 0)
                self.assertTrue((attempt.path / "junit.xml").exists())

    def test_invalid_selection_and_bad_collection_fail(self):
        for target in (
            None,
            "",
            "-q",
            "@targets",
            "../aws_tests/",
            "aws_tests/../test.py",
            "/outside/test.py",
            "aws_tests/missing.py",
        ):
            with self.subTest(target=target), self.assertRaises((ValueError, OSError)):
                testing.TestAttempt(self.run, target, 10)
        for source in (
            "",
            "broken syntax !!!",
            "import pytest\npytest.skip('skip module', allow_module_level=True)",
        ):
            self.file.write_text(source)
            attempt = testing.TestAttempt(self.run, "aws_tests/test_cases.py", 10)
            with self.subTest(source=source), self.assertRaises(RuntimeError):
                attempt.collect()
            self.assertEqual(attempt.result["collection"]["status"], "failed")
            self.assertEqual(attempt.result["execution"]["status"], "not_run")

    def test_fail_skip_and_xfail_never_count_as_success(self):
        self.file.write_text("""import pytest

def test_pass(): pass
def test_fail(): assert False
@pytest.mark.skip(reason='skip')
def test_skip(): pass
@pytest.mark.xfail(reason='expected')
def test_xfail(): assert False
@pytest.mark.xfail(reason='unexpected')
def test_xpass(): pass
""")
        attempt = self.attempt()
        with self.assertRaises(RuntimeError):
            attempt.execute("unused")
        statuses = {
            case["name"].split("::")[-1]: case["status"] for case in attempt.cases()
        }
        self.assertEqual(statuses["test_pass"], "passed")
        for name in ("test_fail", "test_skip", "test_xfail", "test_xpass"):
            self.assertNotEqual(statuses[name], "passed")
        for name in ("test_skip", "test_xfail", "test_xpass"):
            with self.subTest(name=name):
                single = self.attempt("aws_tests/test_cases.py::" + name)
                with self.assertRaisesRegex(
                    RuntimeError, "selected_tests_must_all_pass"
                ):
                    single.execute("unused")
                self.assertEqual(single.result["execution"]["exit_code"], 0)
                self.assertEqual(single.result["execution"]["status"], "failed")

    def test_retry_uses_current_code_and_preserves_previous_snapshot(self):
        (self.source / ".env").write_text("excluded")
        (self.source / "debug.log").write_text("excluded")
        (self.source / "fixture.txt").write_text("fixture")
        first = self.attempt()
        saved = (first.code / "aws_tests/test_cases.py").read_bytes()
        self.file.write_text("def test_changed(): pass\n")
        first.execute("unused")
        result = (first.path / "test-results.json").read_bytes()
        second = self.attempt()
        second.execute("unused")
        self.assertEqual((first.code / "aws_tests/test_cases.py").read_bytes(), saved)
        self.assertEqual((first.path / "test-results.json").read_bytes(), result)
        self.assertTrue(second.cases()[0]["name"].endswith("::test_changed"))
        self.assertFalse((first.code / "aws_tests/.env").exists())
        self.assertFalse((first.code / "aws_tests/debug.log").exists())
        self.assertEqual((first.code / "aws_tests/fixture.txt").read_text(), "fixture")
        self.assertNotEqual(
            json.loads((first.path / "source.json").read_text())["files"],
            json.loads((second.path / "source.json").read_text())["files"],
        )
        (self.source / "outside.py").symlink_to(self.root / "backend/.venv/pyvenv.cfg")
        with self.assertRaisesRegex(ValueError, "outside_aws_tests"):
            testing.TestAttempt(self.run, "aws_tests/outside.py", 10)
        with self.assertRaisesRegex(ValueError, "outside_aws_tests"):
            self.attempt()

    def test_timeout_keeps_unfinished_cases_and_shared_deadline(self):
        for invalid in ("0", "-1", "1.5", "nan", ""):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                testing.timeout_seconds(invalid)
        account = "123456789012"
        prefix = "vector-test-sample"
        outputs = {
            "run": {
                "run_id": "sample",
                "account_id": account,
                "region": "ap-northeast-1",
                "backend_image": f"{account}.dkr.ecr.ap-northeast-1.amazonaws.com"
                "/vector-test/backend@sha256:" + "a" * 64,
            },
            "database": {
                "identifier": prefix,
                "name": "vector",
                "port": 5432,
                "address": f"{prefix}.test.ap-northeast-1.rds.amazonaws.com",
            },
            "execution": {
                "queue_url": f"https://sqs.ap-northeast-1.amazonaws.com/{account}/{prefix}-embedding",
                "log_groups": {
                    name: f"/vector-test/{prefix}/{name}"
                    for name in ("lambda", "runner")
                },
                "instance_ids": {"runner": "i-" + "a" * 17},
            },
        }
        (self.run / "outputs.json").write_text(
            json.dumps({key: {"value": value} for key, value in outputs.items()})
        )
        (self.run / "account.json").write_text(
            json.dumps({"expected_account_id": account})
        )
        self.file.write_text("""import time
from unittest.mock import Mock
import pytest

@pytest.fixture(scope='session', autouse=True)
def fake_aws():
    from unittest.mock import patch
    client = Mock()
    client.get_caller_identity.return_value = {
        'Account': '123456789012',
        'Arn': ('arn:aws:sts::123456789012:assumed-role/'
                'AWSReservedSSO_VectorTestRunner_test/test'),
    }
    with patch('aws_tests.embedding.runtime.Session') as session:
        session.return_value.create_client.return_value = client
        yield

def test_deadline(embedding_runtime, aws_smoke_deadline):
    assert embedding_runtime.deadline == aws_smoke_deadline
    assert 0 < embedding_runtime.remaining() <= 1
    time.sleep(10)

def test_unstarted(): pass
""")
        attempt = self.attempt(timeout=1)
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            attempt.execute("unused")
        self.assertLess(time.monotonic() - start, 8)
        statuses = {
            case["name"].split("::")[-1]: case["status"] for case in attempt.cases()
        }
        self.assertNotEqual(statuses["test_deadline"], "passed")
        self.assertEqual(statuses["test_unstarted"], "not_run")
        self.assertIsNotNone(attempt.result["execution"]["exit_code"])
        self.assertEqual(attempt.result["execution"]["error_type"], "TimeoutExpired")


class ControlTests(unittest.TestCase):
    def setUp(self):
        fixture = up_tests.RunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.run = fixture.run
        fixture.provisioned()
        for phase in ("up", "prepare", "database"):
            self.run.result["phases"][phase] = {"status": "passed"}
        self.attempts = []

        def make_attempt(directory, target, timeout):
            attempt = Mock()
            attempt.path = directory / "test-attempts" / str(len(self.attempts) + 1)
            attempt.path.mkdir(parents=True)
            attempt.target, attempt.timeout = target, timeout
            attempt.result = {"status": "running", "target": target}
            attempt.cases.return_value = []
            self.attempts.append(attempt)
            return attempt

        self.factory = patch.object(
            testing, "TestAttempt", side_effect=make_attempt
        ).start()

    def execute(self, *, create=False):
        with patch("sys.stdout", new=io.StringIO()):
            self.run.execute_tests("aws_tests/example.py", 600, create=create)

    def test_individual_execution_keeps_environment_on_success_failure_and_interrupt(
        self,
    ):
        self.execute()
        self.run.preflight.assert_called_with(resume=True)
        self.attempts[-1].execute.assert_called_once_with("runner")
        for error in (RuntimeError("test_failed"), KeyboardInterrupt()):
            original = self.factory.side_effect

            def fail(*args):
                attempt = original(*args)
                attempt.execute.side_effect = error
                return attempt

            self.factory.side_effect = fail
            with (
                self.subTest(error=type(error).__name__),
                self.assertRaises(type(error)),
            ):
                self.execute()
            self.factory.side_effect = original
            self.assertEqual(self.attempts[-1].result["status"], "failed")
        self.run.provision.assert_not_called()
        self.run.destroy.assert_not_called()
        self.assertEqual(self.run.collect.call_count, 3)
        self.assertEqual(len(self.run.result["test_attempts"]), 3)

    def test_prerequisites_access_and_collection_failures_stop_execution(self):
        original = copy.deepcopy(self.run.result)
        for phase, status in (
            ("up", "failed"),
            ("prepare", "failed"),
            ("database", "failed"),
            ("destroy", "running"),
            ("cleanup_access", "failed"),
        ):
            self.run.result = copy.deepcopy(original)
            self.run.result["phases"][phase] = {"status": status}
            with self.subTest(phase=phase), self.assertRaises(RuntimeError):
                self.execute()
            self.attempts[-1].execute.assert_not_called()
        self.run.preflight.assert_not_called()
        for boundary in (self.run.preflight, self.run.verify_resources):
            self.run.result = copy.deepcopy(original)
            boundary.side_effect = RuntimeError("scope_mismatch")
            with self.assertRaisesRegex(RuntimeError, "scope_mismatch"):
                self.execute()
            boundary.side_effect = None
            self.attempts[-1].execute.assert_not_called()
        self.run.provision.assert_not_called()
        self.run.destroy.assert_not_called()

    def test_one_shot_selection_failure_never_starts_aws_and_runtime_failure_cleans_up(
        self,
    ):
        self.run.result.update(apply_attempted=False, apply_completed=False)
        factory = self.factory.side_effect

        def bad_collection(*args):
            attempt = factory(*args)
            attempt.collect.side_effect = RuntimeError("collection_failed")
            return attempt

        self.factory.side_effect = bad_collection
        with self.assertRaisesRegex(RuntimeError, "collection_failed"):
            self.execute(create=True)
        self.run.preflight.assert_not_called()
        self.run.provision.assert_not_called()
        self.run.destroy.assert_not_called()
        self.factory.side_effect = factory
        with (
            patch.object(controller.assets, "prepare_assets"),
            patch.object(
                self.run,
                "prepare_database",
                side_effect=RuntimeError("database_failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "database_failed"):
                self.execute(create=True)
        self.run.destroy.assert_called_once()
        self.run.collect.assert_called_once()

    def test_log_collection_failure_is_separate_and_keeps_environment(self):
        self.run.collect.side_effect = RuntimeError("logs_failed")
        with self.assertRaisesRegex(RuntimeError, "logs_failed"):
            self.execute()
        self.assertEqual(self.run.result["phases"]["test"]["status"], "passed")
        self.assertEqual(self.run.result["phases"]["test_run"]["status"], "failed")
        self.run.destroy.assert_not_called()

    def test_cli_missing_target_or_timeout_and_parallel_operation_are_rejected(self):
        target = "aws_tests/embedding/test_event_processing.py"
        with (
            patch.object(controller, "LOCAL", self.run.directory.parent.parent),
            patch.object(controller, "Run") as run,
            patch("sys.stderr", new=io.StringIO()),
            patch("sys.stdout", new=io.StringIO()),
        ):
            for action in ("run", "test"):
                for args in (["--test", ""], ["--test", target, "--timeout", "0"]):
                    with (
                        self.subTest(action=action, args=args),
                        patch.object(
                            sys,
                            "argv",
                            ["aws-smoke.py", action, "--run-id", "sample", *args],
                        ),
                        self.assertRaises(SystemExit),
                    ):
                        controller.main()
            with (self.run.directory / "operation.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with (
                    patch.object(
                        sys,
                        "argv",
                        [
                            "aws-smoke.py",
                            "test",
                            "--run-id",
                            "sample",
                            "--test",
                            target,
                        ],
                    ),
                    self.assertRaises(BlockingIOError),
                ):
                    controller.main()
            run.assert_not_called()
