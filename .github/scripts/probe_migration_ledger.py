"""明示実行時のみ検証専用namespaceへ記録を残し、AWS・DBは操作しない。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from migration_ledger import (
    PROBE_LEDGER,
    GitHubCli,
    LedgerError,
    LedgerRecord,
    MigrationLedger,
    migration_tree_oid,
)


def probe(repo: str, release_sha: str) -> dict[str, object]:
    client = GitHubCli(repo)
    subject = MigrationLedger(client, PROBE_LEDGER)
    tree = migration_tree_oid(Path(__file__).resolve().parents[2], release_sha)
    deployment_ids: list[int] = []
    for completion in ("success", "success", "failure"):
        prepared = subject.read_latest()
        if prepared.baseline is None:
            raise LedgerError("probe_baseline_unobservable")
        record = LedgerRecord(
            schema_version=1,
            release_sha=release_sha,
            mode="verify",
            expected_start_revision=None,
            target_revision="ledger_probe_target",
            migration_tree_oid=tree,
            # probeはActions runではないため、このnamespace内だけ合成値を使う。
            github_run_id=1,
            github_run_attempt=1,
            baseline_deployment_id=prepared.baseline.deployment_id,
            baseline_status_id=prepared.baseline.status_id,
        )
        started = subject.begin(prepared, record)
        completed = subject.finish(started, completion)
        if completed.record != record:
            raise LedgerError("probe_payload_round_trip_failed")
        deployment_ids.append(started.deployment_id)

    previous = client.get_json(
        f"/deployments/{deployment_ids[0]}/statuses?per_page=100"
    )
    if (
        not isinstance(previous, list)
        or not previous
        or not all(isinstance(status, dict) for status in previous)
        or any(status.get("state") == "inactive" for status in previous)
        or not any(status.get("state") == "success" for status in previous)
    ):
        raise LedgerError("probe_previous_success_not_preserved")
    latest = subject.read_latest()
    if latest.state != "unavailable" or latest.latest_status != "failure":
        raise LedgerError("probe_failure_not_blocking")
    return {
        "environment": PROBE_LEDGER.environment,
        "task": PROBE_LEDGER.task,
        "deployment_ids": deployment_ids,
        "storage": "payload",
        "payload_round_trip": True,
        "new_failure_unavailable": True,
        "previous_success_not_inactive": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub owner/repository")
    parser.add_argument("--release-sha", required=True, help="既存commitの40桁SHA")
    parser.add_argument(
        "--confirm-write",
        required=True,
        action="store_true",
        help="検証専用Deploymentを3件作り、履歴として残すことに同意する",
    )
    args = parser.parse_args()
    try:
        print(json.dumps(probe(args.repo, args.release_sha), sort_keys=True))
    except LedgerError as exc:
        print(f"ledger probe failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
