"""prepareの操作境界と再実行で誤って成功しない条件を検証する。"""

import copy
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import test_smoke_up as up_tests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from smoke_runner import assets, controller, remote  # noqa: E402


class PrepareTests(unittest.TestCase):
    def setUp(self):
        fixture = up_tests.RunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.run = fixture.run
        fixture.provisioned()
        self.run.result["phases"]["up"] = {"status": "passed"}
        self.assets = patch.object(assets, "prepare_assets").start()
        self.image = patch.object(
            remote, "ensure_image", return_value={"status": "available"}
        ).start()
        self.database = patch.object(
            remote, "prepare", return_value={"status": "prepared"}
        ).start()

    def prepare(self):
        with redirect_stdout(io.StringIO()):
            self.run.prepare()

    def test_prepare_keeps_environment_and_archives_retry_failure(self):
        self.prepare()
        archive = self.run.directory / "prepare-attempts/1/result.json"
        original = archive.read_bytes()
        for error in (RuntimeError("database_connection_failed"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                self.database.side_effect = error
                with self.assertRaises(type(error)):
                    self.prepare()
                self.assertNotIn("database", self.run.result)
                self.assertEqual(
                    self.run.result["phases"]["database"]["status"], "failed"
                )
                self.assertEqual(
                    self.run.result["phases"]["prepare"]["status"], "failed"
                )
                self.assertEqual(archive.read_bytes(), original)
        self.run.provision.assert_not_called()
        self.run.destroy.assert_not_called()
        self.assertEqual(self.run.result["phases"]["test"]["status"], "not_run")
        self.assertEqual(len(self.run.result["prepare_attempts"]), 3)
        self.run.preflight.assert_called_with(resume=True)

    def test_invalid_run_states_and_failed_access_never_reach_preparation(self):
        original = copy.deepcopy(self.run.result)
        for phase, status in (
            ("up", "failed"),
            ("destroy", "passed"),
            ("cleanup_access", "failed"),
            ("verification", "running"),
        ):
            with self.subTest(phase=phase):
                self.run.result = copy.deepcopy(original)
                self.run.result["phases"][phase] = {"status": status}
                with self.assertRaises(RuntimeError):
                    self.prepare()
        self.run.result = copy.deepcopy(original)
        self.run.result["apply_completed"] = False
        with self.assertRaises(RuntimeError):
            self.prepare()
        for boundary in (self.run.preflight, self.run.verify_resources):
            self.run.result = copy.deepcopy(original)
            boundary.side_effect = RuntimeError("scope_mismatch")
            with self.assertRaisesRegex(RuntimeError, "scope_mismatch"):
                self.prepare()
            boundary.side_effect = None
        self.assets.assert_not_called()
        self.image.assert_not_called()
        self.database.assert_not_called()
        self.run.provision.assert_not_called()
        self.run.destroy.assert_not_called()

    def test_cli_requires_existing_run_id_before_loading_or_creating_run(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(controller, "LOCAL", Path(temporary)),
            patch.object(controller, "Run") as run,
            patch.object(controller.snapshot, "create") as create,
        ):
            for run_id in ("", "../wrong", "missing"):
                with (
                    self.subTest(run_id=run_id),
                    patch.object(
                        sys, "argv", ["aws-smoke.py", "prepare", "--run-id", run_id]
                    ),
                    redirect_stdout(io.StringIO()),
                    patch("sys.stderr", new=io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    controller.main()
            run.assert_not_called()
            create.assert_not_called()
            self.assertEqual(list(Path(temporary).iterdir()), [])


class AssetTests(unittest.TestCase):
    def test_generation_retry_reuses_only_complete_unchanged_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            sentinel = directory / "result.json"
            sentinel.write_text("retain")

            def generate(work, revision, name):
                for filename in assets.ASSET_FILES:
                    path = work / filename
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("DDL")

            def interrupted(work, revision, name):
                (work / "source.log").write_text("failed generation")
                raise RuntimeError("generation_failed")

            with patch.object(assets, "_generate_assets", side_effect=interrupted):
                with self.assertRaisesRegex(RuntimeError, "generation_failed"):
                    assets.prepare_assets(directory, "revision")
            self.assertFalse((directory / "prepared-assets").exists())
            self.assertEqual(len(list(directory.glob("assets-failed-*/source.log"))), 1)
            with patch.object(
                assets, "_generate_assets", side_effect=generate
            ) as build:
                assets.prepare_assets(directory, "revision")
                assets.prepare_assets(directory, "revision")
                build.assert_called_once()
                with self.assertRaisesRegex(RuntimeError, "prepared_assets_changed"):
                    assets.prepare_assets(directory, "other-revision")
                (directory / "prepared-assets/auth.sql").write_text("changed")
                with self.assertRaisesRegex(RuntimeError, "prepared_assets_changed"):
                    assets.prepare_assets(directory, "revision")
                build.assert_called_once()
            self.assertEqual(sentinel.read_text(), "retain")


class ImageTests(unittest.TestCase):
    def test_image_pull_reuse_and_digest_mismatch_execute_the_shell_branch(self):
        image = (
            "123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/vector-test/backend@sha256:"
            + "a" * 64
        )
        outputs = {"run": {"backend_image": image}}
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            (work / "proxy.env").write_text("")
            docker = work / "docker"
            docker.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = image ]; then\n'
                '  if [ ! -f "$TEST_WORK/installed" ]; then exit 1; fi\n'
                '  if [ "$3" = --format ]; then cat "$TEST_WORK/digests"; fi\n'
                'elif [ "$1" = pull ]; then\n'
                '  touch "$TEST_WORK/installed"\n'
                '  echo pull >> "$TEST_WORK/pulls"\n'
                'elif [ "$1" = login ]; then cat >/dev/null; fi\n'
            )
            docker.chmod(0o700)
            aws = work / "aws"
            aws.write_text("#!/bin/sh\necho temporary-login\n")
            aws.chmod(0o700)
            (work / "digests").write_text(json.dumps([image]))

            def send(aws, outputs, command, timeout, journal):
                command = command.replace(
                    "/etc/vector-test/proxy.env", str(work / "proxy.env")
                ).replace(
                    "/var/lib/vector-test/backend-image-pull.log",
                    str(work / "pull.log"),
                )
                return subprocess.run(  # noqa: S603
                    shlex.split(command),
                    env={
                        **os.environ,
                        "PATH": f"{work}:/usr/bin:/bin",
                        "TEST_WORK": str(work),
                    },
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=10,
                ).stdout  # noqa: S603

            with patch.object(remote, "send_command", side_effect=send):
                remote.ensure_image(None, outputs, Mock())
                remote.ensure_image(None, outputs, Mock())
                self.assertEqual((work / "pulls").read_text(), "pull\n")
                (work / "digests").write_text(json.dumps(["different-digest"]))
                with self.assertRaisesRegex(
                    RuntimeError, "backend_image_digest_unconfirmed"
                ):
                    remote.ensure_image(None, outputs, Mock())

    def test_database_process_and_report_must_both_succeed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for filename in assets.ASSET_FILES:
                path = directory / "prepared-assets" / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("DDL")
            docker = directory / "docker"
            docker.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$TEST_REPORT"\nexit "$TEST_EXIT_CODE"\n'
            )
            docker.chmod(0o700)
            success = {
                "status": "prepared",
                "heads": ["head"],
                "verified": True,
                "action": "verified",
            }
            outputs = {
                "run": {"backend_image": "registry/image@sha256:fixed"},
                "database": {},
            }
            report, exit_code = json.dumps(success), "0"
            timeout_command = directory / "timeout"
            timeout_command.write_text('#!/bin/sh\nshift 3\nexec "$@"\n')
            timeout_command.chmod(0o700)

            def send(aws, outputs, command, timeout, journal):
                return subprocess.run(  # noqa: S603
                    [
                        "/bin/bash",
                        "-c",
                        command.replace("/var/lib/vector-test", str(directory)),
                    ],
                    env={
                        **os.environ,
                        "PATH": f"{directory}:/usr/bin:/bin",
                        "TEST_REPORT": report,
                        "TEST_EXIT_CODE": exit_code,
                    },
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=10,
                ).stdout

            with patch.object(remote, "send_command", side_effect=send):
                self.assertEqual(
                    remote.prepare(None, outputs, directory, Mock()), success
                )
                exit_code = "1"
                with self.assertRaisesRegex(
                    RuntimeError, "database_preparation_unconfirmed"
                ):
                    remote.prepare(None, outputs, directory, Mock())
                report = json.dumps(
                    {"status": "failed", "reason": "database_prepare_in_progress"}
                )
                with self.assertRaisesRegex(
                    RuntimeError, "database_prepare_in_progress"
                ):
                    remote.prepare(None, outputs, directory, Mock())
                report = ""
                with self.assertRaisesRegex(RuntimeError, "database_process_failed"):
                    remote.prepare(None, outputs, directory, Mock())
