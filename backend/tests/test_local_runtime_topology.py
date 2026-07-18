"""ローカル常駐workerの運用集合を固定するstatic contract tests。"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_MAKEFILE = _REPOSITORY_ROOT / "Makefile"
_COMPOSE_FILE = _REPOSITORY_ROOT / "docker-compose.yml"


def _makefile_words(variable: str) -> set[str]:
    match = re.search(
        rf"^{re.escape(variable)}\s*:?=\s*(?P<value>[^\n]+)$",
        _MAKEFILE.read_text(),
        flags=re.MULTILINE,
    )
    assert match is not None, f"Makefile variable {variable} is missing"
    return set(match.group("value").split())


def _compose_runtime_workers() -> set[str]:
    services: set[str] = set()
    inside_services = False
    for line in _COMPOSE_FILE.read_text().splitlines():
        if line == "services:":
            inside_services = True
            continue
        if inside_services and line and not line.startswith((" ", "#")):
            break
        if not inside_services:
            continue
        match = re.fullmatch(r"  (?P<service>[a-z0-9][a-z0-9-]*):", line)
        if match is not None:
            services.add(match.group("service"))
    return {
        service
        for service in services
        if service.startswith("worker-") or service == "scheduler"
    }


def _make_dry_run(target: str) -> list[str]:
    make = shutil.which("make")
    assert make is not None, "make executable is missing"
    completed = subprocess.run(  # noqa: S603
        [make, "--no-print-directory", "-n", target],
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.splitlines()


def _commands_starting_with(target: str, prefix: list[str]) -> list[list[str]]:
    rendered_prefix = " ".join(prefix)
    return [
        shlex.split(line)
        for line in _make_dry_run(target)
        if line.startswith(rendered_prefix)
    ]


def test_makefile_workers_match_all_compose_runtime_workers() -> None:
    assert _makefile_words("WORKERS") == _compose_runtime_workers()


@pytest.mark.parametrize(
    ("target", "command_prefixes"),
    [
        ("pipeline-restart", [["docker", "compose", "up", "-d"]]),
        ("pipeline-down", [["docker", "compose", "stop"]]),
        ("pipeline-logs", [["docker", "compose", "logs"]]),
        (
            "migrate-safe",
            [
                ["docker", "compose", "stop"],
                ["docker", "compose", "up", "-d"],
            ],
        ),
    ],
)
def test_worker_agent_is_in_every_default_worker_target(
    target: str,
    command_prefixes: list[list[str]],
) -> None:
    expanded_commands = [
        command
        for prefix in command_prefixes
        for command in _commands_starting_with(target, prefix)
    ]

    assert expanded_commands and all(
        "worker-agent" in command for command in expanded_commands
    )


def test_pipeline_restart_force_recreates_backend_and_every_runtime_worker() -> None:
    commands = _commands_starting_with(
        "pipeline-restart",
        ["docker", "compose", "up", "-d", "--force-recreate"],
    )
    assert len(commands) == 1
    force_recreated = set(commands[0][5:])

    assert force_recreated == {"backend", *_compose_runtime_workers()}
