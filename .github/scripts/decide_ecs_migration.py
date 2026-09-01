"""本番migration one-off taskを起動するか決め、成功時にledgerを書く。"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

SUCCESS = 0
FAILURE = 1

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
LEDGER_ENVIRONMENT = "db-migration"
LEDGER_TASK = "deploy:migrations"
VERSIONS_PATH = "backend/alembic/versions"


class DecideInputError(ValueError):
    """判定入力またはgit/GitHub responseの契約違反。"""


class GitHubApi(Protocol):
    def get_json(self, path: str) -> object: ...

    def post_json(self, path: str, body: Mapping[str, object]) -> Mapping[str, object]: ...


def require_sha(value: str, *, field: str) -> str:
    if not _SHA_RE.fullmatch(value):
        raise DecideInputError(f"{field} は40桁の小文字16進commit SHAで指定してください")
    return value


def parse_force(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized in {"false", ""}:
        return False
    raise DecideInputError("--force は true または false で指定してください")


def has_commit(repo: Path, sha: str) -> bool:
    completed = subprocess.run(  # noqa: S603
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
        cwd=repo,
        check=False,
        capture_output=True,
    )
    return completed.returncode == 0


def versions_differ(repo: Path, last_sha: str, release_sha: str, versions_path: str) -> bool:
    completed = subprocess.run(  # noqa: S603
        ["git", "diff", "--quiet", last_sha, release_sha, "--", versions_path],
        cwd=repo,
        check=False,
        capture_output=True,
    )
    if completed.returncode == 0:
        return False
    if completed.returncode == 1:
        return True
    detail = completed.stderr.decode("utf-8", errors="replace").strip()
    raise DecideInputError(f"git diff に失敗した: {detail or completed.returncode}")


def should_run_migration(
    *,
    repo: Path,
    release_sha: str,
    last_migrated_sha: str | None,
    force: bool,
    versions_path: str = VERSIONS_PATH,
) -> bool:
    require_sha(release_sha, field="release_sha")
    if force:
        return True
    if last_migrated_sha is None:
        return True
    require_sha(last_migrated_sha, field="last_migrated_sha")
    if not has_commit(repo, last_migrated_sha):
        return True
    return versions_differ(repo, last_migrated_sha, release_sha, versions_path)


def latest_successful_sha(
    deployments: Sequence[Mapping[str, object]],
    statuses_by_id: Mapping[int, Sequence[Mapping[str, object]]],
) -> str | None:
    for deployment in deployments:
        if deployment.get("environment") != LEDGER_ENVIRONMENT:
            continue
        if deployment.get("task") != LEDGER_TASK:
            continue
        deployment_id = deployment.get("id")
        if not isinstance(deployment_id, int):
            continue
        statuses = statuses_by_id.get(deployment_id, ())
        if not statuses:
            continue
        latest = statuses[0]
        if latest.get("state") != "success":
            continue
        sha = deployment.get("sha")
        if isinstance(sha, str) and _SHA_RE.fullmatch(sha):
            return sha
    return None


def ledger_create_body(release_sha: str) -> dict[str, object]:
    require_sha(release_sha, field="release_sha")
    return {
        "ref": release_sha,
        "environment": LEDGER_ENVIRONMENT,
        "task": LEDGER_TASK,
        "auto_merge": False,
        "required_contexts": [],
        "production_environment": False,
    }


def fetch_last_successful_sha(client: GitHubApi) -> str | None:
    deployments = client.get_json(
        f"/deployments?environment={LEDGER_ENVIRONMENT}&per_page=100"
    )
    if not isinstance(deployments, list):
        raise DecideInputError("deployments の応答が配列ではない")
    statuses_by_id: dict[int, Sequence[Mapping[str, object]]] = {}
    typed: list[Mapping[str, object]] = []
    for item in deployments:
        if not isinstance(item, Mapping):
            raise DecideInputError("deployment の要素がobjectではない")
        typed.append(item)
        deployment_id = item.get("id")
        if not isinstance(deployment_id, int):
            continue
        statuses = client.get_json(f"/deployments/{deployment_id}/statuses?per_page=1")
        if not isinstance(statuses, list):
            raise DecideInputError("deployment statuses の応答が配列ではない")
        mapped: list[Mapping[str, object]] = []
        for status in statuses:
            if not isinstance(status, Mapping):
                raise DecideInputError("deployment status の要素がobjectではない")
            mapped.append(status)
        statuses_by_id[deployment_id] = mapped
    return latest_successful_sha(typed, statuses_by_id)


def record_successful_migration(client: GitHubApi, release_sha: str) -> None:
    created = client.post_json("/deployments", ledger_create_body(release_sha))
    deployment_id = created.get("id")
    if not isinstance(deployment_id, int):
        raise DecideInputError("created deployment id が整数ではない")
    client.post_json(f"/deployments/{deployment_id}/statuses", {"state": "success"})


class SubprocessGitHubApi:
    """gh api で Deployments REST を呼ぶ。"""

    def __init__(self, repo: str, gh_path: str) -> None:
        self._repo = repo
        self._gh_path = gh_path

    def get_json(self, path: str) -> object:
        return self._request(["--method", "GET", f"repos/{self._repo}{path}"])

    def post_json(self, path: str, body: Mapping[str, object]) -> Mapping[str, object]:
        payload = self._request(
            ["--method", "POST", "--input", "-", f"repos/{self._repo}{path}"],
            stdin=json.dumps(body),
        )
        if not isinstance(payload, Mapping):
            raise DecideInputError("GitHub POST の応答がobjectではない")
        return payload

    def _request(self, args: Sequence[str], *, stdin: str | None = None) -> object:
        completed = subprocess.run(  # noqa: S603
            [self._gh_path, "api", *args],
            input=stdin,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise DecideInputError(f"gh api が失敗した: {detail}")
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise DecideInputError("gh api の応答がJSONではない") from exc


def _write_output(path: Path, **values: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    decide = subparsers.add_parser("decide")
    decide.add_argument("--release-sha", required=True)
    decide.add_argument("--last-migrated-sha", default="")
    decide.add_argument("--force", default="false")
    decide.add_argument("--repo-dir", type=Path, default=Path.cwd())
    decide.add_argument("--versions-path", default=VERSIONS_PATH)
    decide.add_argument("--github-repo", default="")
    decide.add_argument("--output", type=Path, required=True)

    record = subparsers.add_parser("record")
    record.add_argument("--release-sha", required=True)
    record.add_argument("--github-repo", required=True)

    return parser.parse_args(argv)


def _github_api(github_repo: str) -> SubprocessGitHubApi:
    path = shutil.which("gh")
    if path is None:
        raise DecideInputError("gh executable was not found")
    if not github_repo:
        raise DecideInputError("--github-repo が空")
    return SubprocessGitHubApi(github_repo, path)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "decide":
            force = parse_force(args.force)
            last = args.last_migrated_sha.strip() or None
            if not force and last is None and args.github_repo:
                last = fetch_last_successful_sha(_github_api(args.github_repo))
            run = should_run_migration(
                repo=args.repo_dir,
                release_sha=args.release_sha,
                last_migrated_sha=last,
                force=force,
                versions_path=args.versions_path,
            )
            _write_output(args.output, run="true" if run else "false")
            return SUCCESS
        record_successful_migration(
            _github_api(args.github_repo),
            args.release_sha,
        )
        return SUCCESS
    except DecideInputError as exc:
        print(f"::error::{exc}")
        return FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
