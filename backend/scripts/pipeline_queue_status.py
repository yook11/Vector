"""acquisition / completion / curation / assessment の4 stageを表示する
operator向けRedis Stream status。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from redis.asyncio import Redis

from app.queue.stream_health import (
    PIPELINE_QUEUE_TARGETS,
    StreamHealthError,
    StreamHealthSnapshot,
    StreamHealthTarget,
    has_idle_pending,
    read_stream_health,
)
from app.redis import get_redis

_HEADER = (
    "Stream Retained Lag Pending Oldest_undelivered_enqueue_age "
    "Oldest_pending_enqueue_age Oldest_outstanding_enqueue_age Status"
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """CLI引数を解析する。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-idle",
        action="store_true",
        help="idleが600秒以上のpending entryの存在だけを追加診断する",
    )
    return parser.parse_args(argv)


def _format_age(age: float | None) -> str:
    """age未計測をdash、計測値を小数3桁以内の秒で表示する。"""
    if age is None:
        return "-"
    return f"{age:.3f}".rstrip("0").rstrip(".")


def _snapshot_row(snapshot: StreamHealthSnapshot) -> str:
    """正常snapshotを1行へ整形する。"""
    values = (
        snapshot.stream,
        str(snapshot.retained_entries),
        str(snapshot.lag),
        str(snapshot.pending),
        _format_age(snapshot.oldest_undelivered_enqueue_age),
        _format_age(snapshot.oldest_pending_enqueue_age),
        _format_age(snapshot.oldest_outstanding_enqueue_age),
        "ok",
    )
    return " ".join(values)


def _error_status(error: StreamHealthError) -> str:
    """固定failure reasonをoperator向けstatusへ写像する。"""
    if error.reason in {"stream_missing", "group_missing"}:
        return "unavailable"
    if error.reason == "lag_unknown":
        return "unknown"
    return "failure"


def _error_row(target: StreamHealthTarget, error: StreamHealthError) -> str:
    """観測失敗を0件と混同しない1行へ整形する。"""
    return " ".join((target.stream, *("-" for _ in range(6)), _error_status(error)))


async def render_pipeline_queue_status(
    redis: Redis,
    *,
    check_idle: bool = False,
) -> str:
    """共有snapshot helperだけを使ってstage別statusを生成する。"""
    rows = [_HEADER]
    idle_notes: list[str] = []
    for target in PIPELINE_QUEUE_TARGETS:
        try:
            snapshot = await read_stream_health(redis, target)
        except StreamHealthError as error:
            rows.append(_error_row(target, error))
            continue

        rows.append(_snapshot_row(snapshot))
        if check_idle:
            try:
                idle_exists = await has_idle_pending(
                    redis,
                    target,
                    idle_ms=600_000,
                )
            except StreamHealthError as error:
                idle_notes.append(
                    f"idle diagnostic {target.stream}: {_error_status(error)}"
                )
            else:
                existence = "exists" if idle_exists else "absent"
                idle_notes.append(f"idle>=600s entry {existence}: {target.stream}")

    return "\n".join((*rows, *idle_notes))


async def _run(check_idle: bool) -> None:
    """共有Redis clientでstatusを取得して標準出力へ表示する。"""
    print(await render_pipeline_queue_status(get_redis(), check_idle=check_idle))


def main(argv: Sequence[str] | None = None) -> None:
    """operator CLIを実行する。"""
    args = parse_args(argv)
    asyncio.run(_run(args.check_idle))


if __name__ == "__main__":
    main()
