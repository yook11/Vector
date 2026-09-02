"""contractの混在制限は変更されたrevisionとPR全体の差分で決まる。"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts import migration_change_gate as gate

pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[3]
_VERSIONS = "backend/alembic/versions/"
_TEST_PATHS = (
    "backend/tests/test_contract.py",
    "backend/tests/conftest.py",
    "backend/tests/fixtures/helper.py",
    "frontend/src/feature.test.ts",
    "frontend/src/nested/feature.spec.tsx",
    "frontend/src/test/mock.ts",
    "frontend/e2e/fixtures/user.ts",
    "frontend/vitest.setup.client.ts",
    "frontend/vitest.setup.node.ts",
    "README.md",
    "docs/migration.md",
)
_RUNTIME_PATHS = (
    "backend/app/service.py",
    "backend/scripts/migration_runner.py",
    ".github/workflows/ci.yml",
    "infra/aws/ecs.tf",
    "backend/pyproject.toml",
    "frontend/vitest.config.ts",
    "frontend/src/feature.test.ts.js",
)


def _git(root: Path, *args: str) -> str:
    git = shutil.which("git")
    assert git is not None
    return subprocess.check_output(  # noqa: S603
        [git, "-C", str(root), *args], text=True, stderr=subprocess.DEVNULL
    ).strip()


def _revision(kind: str) -> str:
    operation = "op.drop_table('obsolete')" if kind == "contract" else "pass"
    return f"MIGRATION_KIND = {kind!r}\ndef upgrade():\n    {operation}\n"


def _commit(root: Path, files: dict[str, str | None]) -> str:
    for name, source in files.items():
        path = root / name
        if source is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "fixture")
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str]:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Migration change test")
    sha = _commit(
        tmp_path,
        {
            f"{_VERSIONS}old.py": _revision("contract"),
            "backend/app/obsolete.py": "old runtime\n",
        },
    )
    return tmp_path, sha


@pytest.mark.parametrize(
    ("kinds", "runtime", "decision", "allowed"),
    [
        ((), False, "none", True),
        ((), True, "none", True),
        (("expand",), True, "expand", True),
        (("contract",), False, "manual", True),
        (("contract",), True, "manual", False),
        (("expand", "contract"), False, "invalid", False),
        (("unknown",), False, "invalid", False),
    ],
)
def test_only_changed_migrations_activate_contract_policy(
    repository: tuple[Path, str],
    kinds: tuple[str, ...],
    runtime: bool,
    decision: str,
    allowed: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, base = repository
    files = {"backend/tests/test_migration.py": "test only\n"}
    files.update(
        {f"{_VERSIONS}r{index}.py": _revision(kind) for index, kind in enumerate(kinds)}
    )
    if runtime:
        files["backend/app/feature.py"] = "runtime\n"
    head = _commit(root, files)
    code = gate.main(["--repo-root", str(root), "--base-sha", base, "--head-sha", head])
    output = json.loads(capsys.readouterr().out)
    assert (code, output["decision"], output["allowed"]) == (
        0 if allowed else 1,
        decision,
        allowed,
    )


@pytest.mark.parametrize("runtime_paths", [(), _RUNTIME_PATHS])
def test_contract_allows_tests_and_helpers_but_not_runtime_or_configuration(
    repository: tuple[Path, str], runtime_paths: tuple[str, ...]
) -> None:
    root, base = repository
    files = dict.fromkeys((*_TEST_PATHS, *runtime_paths), "changed\n")
    files[f"{_VERSIONS}contract.py"] = (
        _revision("contract") + "\nraise RuntimeError('migration must not execute')\n"
    )
    head = _commit(root, files)
    # checkout上の内容ではなく承認対象commitの内容を静的に読む。
    (root / f"{_VERSIONS}contract.py").write_text("raise RuntimeError('not imported')")

    result = gate.check_changes(root, base, head)

    assert (result.allowed, result.rejected_paths) == (
        not runtime_paths,
        tuple(sorted(runtime_paths)),
    )


@pytest.mark.parametrize(
    "change", ["earlier_runtime", "rename_runtime", "contract_only"]
)
@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_ci_uses_the_complete_event_diff_and_propagates_rejection(
    repository: tuple[Path, str],
    tmp_path: Path,
    change: str,
    event: str,
) -> None:
    root, base = repository
    _git(root, "switch", "-c", "feature")
    if change == "earlier_runtime":
        _commit(root, {"backend/app/feature.py": "runtime\n"})
    elif change == "rename_runtime":
        _commit(
            root,
            {
                "backend/app/obsolete.py": None,
                "backend/tests/test_obsolete.py": "old runtime\n",
            },
        )
    head = _commit(root, {f"{_VERSIONS}contract.py": _revision("contract")})
    _git(root, "switch", "main")
    advanced_base = _commit(root, {"backend/app/base_only.py": "not in PR\n"})
    workflow = yaml.safe_load((_ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["migration-check"]["steps"]
    step = next(s for s in steps if "scripts.migration_change_gate" in s.get("run", ""))
    bash = shutil.which("bash")
    assert bash is not None
    result = subprocess.run(  # noqa: S603
        [
            bash,
            "-e",
            "-o",
            "pipefail",
            "-c",
            step["run"].replace("uv run python", shlex.quote(sys.executable)),
        ],
        cwd=root / "backend",
        env={
            **os.environ,
            "PYTHONPATH": str(_ROOT / "backend"),
            "GITHUB_WORKSPACE": str(root),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
            "MIGRATION_EVENT": event,
            "MIGRATION_BASE_SHA": advanced_base if event == "pull_request" else base,
            "MIGRATION_HEAD_SHA": head,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == (0 if change == "contract_only" else 1), result.stderr


@pytest.mark.parametrize("runtime", [False, True])
def test_deleted_revision_is_classified_from_base(
    repository: tuple[Path, str], runtime: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    root, base = repository
    files: dict[str, str | None] = {f"{_VERSIONS}old.py": None}
    if runtime:
        files["backend/app/feature.py"] = "runtime\n"
    head = _commit(root, files)
    code = gate.main(["--repo-root", str(root), "--base-sha", base, "--head-sha", head])
    output = json.loads(capsys.readouterr().out)
    assert (code, output["decision"], output["allowed"]) == (
        1 if runtime else 0,
        "manual",
        not runtime,
    )


def test_renamed_revision_is_classified_from_both_sides(
    repository: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    root, base = repository
    head = _commit(
        root,
        {
            f"{_VERSIONS}old.py": None,
            f"{_VERSIONS}renamed.py": _revision("contract"),
        },
    )
    code = gate.main(["--repo-root", str(root), "--base-sha", base, "--head-sha", head])
    output = json.loads(capsys.readouterr().out)
    assert (code, output["decision"], output["allowed"]) == (0, "manual", True)


@pytest.mark.parametrize("unobservable", ["missing_commit", "symlink"])
def test_unobservable_or_non_regular_migrations_fail_closed(
    repository: tuple[Path, str], unobservable: str, capsys: pytest.CaptureFixture[str]
) -> None:
    root, base = repository
    if unobservable == "missing_commit":
        head = "f" * 40
    else:
        path = root / f"{_VERSIONS}old.py"
        path.unlink()
        path.symlink_to("../../../backend/app/obsolete.py")
        head = _commit(root, {})

    code = gate.main(["--repo-root", str(root), "--base-sha", base, "--head-sha", head])

    assert (code, json.loads(capsys.readouterr().out)["allowed"]) == (1, False)
