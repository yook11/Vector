"""選択したAWSテストのコード・収集結果・実行結果を試行ごとに保存する。"""

import hashlib
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from . import snapshot
from .common import ROOT, execute, save


def now():
    return datetime.now(UTC).isoformat()


def timeout_seconds(value):
    if not re.fullmatch(r"[0-9]+", str(value)) or int(value) <= 0:
        raise ValueError("test_timeout_must_be_positive_seconds")
    return int(value)


def excluded(path):
    return any(
        part.startswith(".")
        or part in {"__pycache__", "node_modules", "logs"}
        or part.endswith((".log", ".pyc", ".pyo"))
        for part in path.parts
    )


def selector(value, root):
    if not value or value.startswith(("-", "@")):
        raise ValueError("test_target_required")
    file, separator, node = value.partition("::")
    path = Path(file)
    if (
        path.is_absolute()
        or not path.parts
        or path.parts[0] != "aws_tests"
        or ".." in path.parts
        or excluded(path)
    ):
        raise ValueError("test_target_outside_aws_tests")
    source = root / "backend/aws_tests"
    if source.is_symlink():
        raise ValueError("test_target_outside_aws_tests")
    target = root / "backend" / path
    resolved = target.resolve(strict=True)
    if not resolved.is_relative_to(source.resolve()) or excluded(
        resolved.relative_to(source.resolve())
    ):
        raise ValueError("test_target_outside_aws_tests")
    if not (target.is_file() or target.is_dir()) or (
        separator and (not node or not target.is_file())
    ):
        raise ValueError("invalid_test_target")
    return path.as_posix() + (separator + node if separator else "")


def copy_tests(source, destination):
    root = source.resolve()

    def copy(path, target, ancestors):
        relative = path.relative_to(source)
        if excluded(relative):
            return
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root) or excluded(resolved.relative_to(root)):
            raise ValueError("test_snapshot_link_outside_aws_tests")
        if resolved.is_dir():
            if resolved in ancestors:
                raise ValueError("test_snapshot_link_cycle")
            target.mkdir(parents=True)
            for child in sorted(path.iterdir()):
                copy(child, target / child.name, {*ancestors, resolved})
        elif resolved.is_file():
            shutil.copyfile(resolved, target)
        else:
            raise ValueError("test_snapshot_requires_regular_files")

    copy(source, destination, set())
    return {
        str(path.relative_to(destination.parent)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(destination.rglob("*"))
        if path.is_file()
    }


class TestAttempt:
    def __init__(self, directory, target, timeout):
        self.run_directory = directory
        self.target = selector(target, ROOT)
        self.timeout = timeout_seconds(timeout)
        parent = directory / "test-attempts"
        number = (
            max((int(p.name) for p in parent.glob("*") if p.name.isdigit()), default=0)
            + 1
        )
        self.path = parent / str(number)
        self.path.mkdir(parents=True)
        self.code = self.path / "code"
        self.result = {
            "number": number,
            "target": self.target,
            "timeout_seconds": self.timeout,
            "started_at": now(),
            "status": "running",
            "nodeids": [],
            "collection": {"status": "not_run", "exit_code": None},
            "execution": {"status": "not_run", "exit_code": None},
            "log_collection": {"status": "not_run"},
        }
        self.persist()

    def persist(self):
        save(self.path / "result.json", self.result)

    def environment(self):
        return {
            **snapshot.environment(self.run_directory),
            "PYTHONPATH": str(self.code),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def arguments(self, report, collection):
        return [
            str(ROOT / "backend/.venv/bin/python"),
            "-m",
            "pytest",
            "-c",
            str(self.code / "pytest.ini"),
            "--rootdir",
            str(self.code),
            "--confcutdir",
            str(self.code),
            "-p",
            "no:cacheprovider",
            "-p",
            "pytest_asyncio.plugin",
            "-p",
            "aws_tests.reporting",
            "--aws-smoke-report",
            str(self.path / report),
            "--aws-smoke-collection",
            str(self.path / collection),
            "-q",
            self.target,
        ]

    def process(self, args, stage, timeout, log):
        record = self.result[stage]
        record.update(status="running", started_at=now())
        self.persist()

        def exited(code):
            record["exit_code"] = code
            self.persist()

        try:
            execute(
                args,
                cwd=self.code,
                env=self.environment(),
                log=self.path / log,
                timeout=timeout,
                progress=True,
                on_exit=exited,
            )
        except BaseException as error:
            record.update(status="failed", error_type=type(error).__name__)
            raise
        else:
            record["status"] = "passed"
        finally:
            record["finished_at"] = now()
            self.persist()

    def collect(self):
        hashes = copy_tests(ROOT / "backend/aws_tests", self.code / "aws_tests")
        (self.code / "pytest.ini").write_text("[pytest]\nasyncio_mode = auto\n")
        hashes["pytest.ini"] = hashlib.sha256(
            (self.code / "pytest.ini").read_bytes()
        ).hexdigest()
        revision = subprocess.run(  # noqa: S603,S607
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        save(self.path / "source.json", {"git_revision": revision, "files": hashes})
        self.process(
            self.arguments("collection-cases.json", "collection.json")
            + ["--collect-only"],
            "collection",
            60,
            "collection.log",
        )
        try:
            collection = snapshot.load(self.path / "collection.json")
            nodes = collection["nodeids"]
            if (
                collection["exit_code"] != 0
                or collection["issues"]
                or not nodes
                or len(nodes) != len(set(nodes))
            ):
                raise RuntimeError("test_collection_empty_or_invalid")
            self.result["nodeids"] = nodes
            save(
                self.path / "test-results.json",
                snapshot.load(self.path / "collection-cases.json"),
            )
            self.persist()
        except Exception as error:
            self.result["collection"].update(
                status="failed", error_type=type(error).__name__
            )
            self.persist()
            raise

    def cases(self):
        path = self.path / "test-results.json"
        return snapshot.load(path) if path.exists() else []

    def execute(self, profile):
        deadline = time.monotonic() + self.timeout
        self.result.update(
            deadline_monotonic=deadline,
            deadline_utc=datetime.fromtimestamp(
                time.time() + self.timeout, UTC
            ).isoformat(),
        )
        self.persist()
        args = self.arguments("test-results.json", "execution-collection.json") + [
            "--aws-smoke-outputs",
            str(self.run_directory / "outputs.json"),
            "--aws-account-config",
            str(self.run_directory / "account.json"),
            "--aws-runner-profile",
            profile,
            "--aws-smoke-deadline",
            str(deadline),
            "-o",
            "junit_family=xunit1",
            "--junitxml=" + str(self.path / "junit.xml"),
        ]
        self.process(
            args, "execution", max(0, deadline - time.monotonic()), "pytest.log"
        )
        try:
            cases = self.cases()
            actual = snapshot.load(self.path / "execution-collection.json")
            expected = set(self.result["nodeids"])
            if (
                not expected
                or actual["exit_code"] != 0
                or actual["issues"]
                or set(actual["nodeids"]) != expected
                or not isinstance(cases, list)
                or len(cases) != len(expected)
                or {case["name"] for case in cases} != expected
                or any(case["status"] != "passed" for case in cases)
            ):
                raise RuntimeError("selected_tests_must_all_pass")
        except Exception as error:
            self.result["execution"].update(
                status="failed", error_type=type(error).__name__
            )
            self.persist()
            raise
