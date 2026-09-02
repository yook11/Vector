"""最新mainと適用済みschemaを確認し、DBや適用記録には書き込まない。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from migration_ledger import GitHubApi, GitHubCli, MigrationLedger
from migration_prepare import (
    GitReleaseRepository,
    PreparationError,
    require_sha,
    require_successful_workflows,
)


class ReleaseGuardError(ValueError):
    pass


def check_app_release(
    repository: GitReleaseRepository,
    github: GitHubApi,
    *,
    release_sha: str,
    check_schema: bool,
) -> dict[str, object]:
    require_sha(release_sha)
    repository.ensure_main(release_sha)
    try:
        require_successful_workflows(github, release_sha)
    except PreparationError:
        raise ReleaseGuardError("required_checks_not_successful") from None
    result: dict[str, object] = {"result": "allowed", "release_sha": release_sha}
    if check_schema:
        schema = repository.schema(release_sha)
        snapshot = MigrationLedger(github).read_latest()
        if not snapshot.allows_rollout(schema.head, schema.tree_oid):
            raise ReleaseGuardError("applied_schema_not_confirmed")
        result.update(
            target_revision=schema.head,
            migration_tree_oid=schema.tree_oid,
            deployment_id=snapshot.baseline.deployment_id,
            status_id=snapshot.baseline.status_id,
        )
    # 他の照合を終えてからmainを読むことで、承認待ち・登録中に進んだrunを拒否する。
    try:
        ref = github.get_json("/git/ref/heads/main")
        if not isinstance(ref, Mapping) or not isinstance(ref.get("object"), Mapping):
            raise ValueError
        current_sha = require_sha(ref["object"].get("sha"))
    except Exception:
        raise ReleaseGuardError("main_ref_unavailable") from None
    if current_sha != release_sha:
        raise ReleaseGuardError("main_has_advanced")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("source", "rollout"))
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--summary-file", type=Path)
    args = parser.parse_args(argv)
    try:
        result = check_app_release(
            GitReleaseRepository(args.repo_root),
            GitHubCli(args.repo),
            release_sha=args.release_sha,
            check_schema=args.phase == "rollout",
        )
        if args.summary_file is not None:
            with args.summary_file.open("a", encoding="utf-8") as stream:
                stream.write(
                    "## App release check\n\n"
                    f"- Release: `{args.release_sha}`\n"
                    "- CI / Security: latest main push workflows succeeded "
                    "(CI may include skipped PR-only tests).\n"
                )
                if args.phase == "rollout":
                    stream.write(
                        f"- Applied revision: `{result['target_revision']}`\n"
                        f"- Migration tree: `{result['migration_tree_oid']}`\n"
                        f"- Ledger: `{result['deployment_id']}` / "
                        f"status `{result['status_id']}`\n"
                    )
        print(json.dumps(result))
        return 0
    except ReleaseGuardError as exc:
        reason = str(exc)
    except Exception:
        reason = "release_guard_failed"
    print(json.dumps({"result": "denied", "reason": reason}), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
