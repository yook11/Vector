"""scheduler routing totality oracle — cron task の broker(queue)割り当て不変条件。

統合 scheduler (``app.queue.scheduler_entrypoint``) は 5 つの stock TaskiqScheduler を
1 プロセスで並行実行する。各 scheduler は自分の broker へ kick するため、cron task →
queue の routing が壊れないことが Option B の前提。本テストはその不変条件を Redis なしで
固定する:

  (a) 各 scheduler の ``LabelScheduleSource`` が自分の broker に属する cron task だけを
      過不足なく発見する。
  (b) 4 scheduler 全体で全 cron task を漏れ・重複なく分割発見する (totality)。
  (c) scheduler を持たない broker に schedule 付き task が無い (orphan cron 検出)。

期待集合は仕様 (``app/queue/schedule.py`` の cron 時刻表 + 各 task module の
``@broker_X.task(schedule=...)`` 帰属) から直書きする。実装出力を期待値にしない。
誤 broker への登録は ``LabelScheduleSource`` の ``task.broker != self.broker`` skip で
発見集合から落ち、(a)/(b)/(c) のいずれかが赤になる。
"""

from __future__ import annotations

import pytest
from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource

import app.queue.registry  # noqa: F401  cron 登録の副作用 import (get_all_tasks を満たす)
from app.queue.brokers import (
    broker_analysis,
    broker_content,
    broker_embedding,
)
from app.queue.schedulers import (
    scheduler_agent,
    scheduler_briefing,
    scheduler_maintenance,
    scheduler_metadata,
    scheduler_trend_discovery,
)

# 仕様 (schedule.py 時刻表 + task module の `@broker_X.task(schedule=...)` 帰属) から
# 直書きした scheduler → 発見されるべき cron task_name 集合。
_EXPECTED_CRON: list[tuple[str, TaskiqScheduler, set[str]]] = [
    (
        "metadata",
        scheduler_metadata,
        {
            "dispatch_high",
            "dispatch_medium",
            "dispatch_low",
            "dispatch_html_fetch_jobs",
            "sweep_expired_leases",
        },
    ),
    ("trend_discovery", scheduler_trend_discovery, {"run_trend_discovery"}),
    ("agent", scheduler_agent, {"sweep_stale_agent_runs"}),
    ("briefing", scheduler_briefing, {"dispatch_weekly_briefings"}),
    (
        "maintenance",
        scheduler_maintenance,
        {
            "backfill_curations",
            "backfill_assessments",
            "backfill_embeddings",
            "observe_pipeline_queue_health",
            "purge_auth_rate_limits",
            "purge_pipeline_events",
        },
    ),
]

# schedule.py の cron 時刻表 (SSoT) が列挙する cron task の総数。新 cron 追加時は
# 時刻表・_EXPECTED_CRON・本定数の 3 点を同時更新する (drift 時に本テストが赤になる)。
_TOTAL_CRON_COUNT = 14


async def _discovered_cron_task_names(scheduler: TaskiqScheduler) -> set[str]:
    """scheduler の全 source を startup し、発見した cron task_name 集合を返す。

    ``LabelScheduleSource.startup`` は ``broker.get_all_tasks()`` のメモリ走査のみで
    Redis 接続を伴わない (``scheduler.startup`` / ``broker.startup`` は呼ばない)。
    """
    names: set[str] = set()
    for source in scheduler.sources:
        await source.startup()
        names |= {task.task_name for task in await source.get_schedules()}
    return names


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "scheduler", "expected"),
    _EXPECTED_CRON,
    ids=[label for label, _, _ in _EXPECTED_CRON],
)
async def test_scheduler_discovers_exactly_its_brokers_cron(
    label: str, scheduler: TaskiqScheduler, expected: set[str]
) -> None:
    """各 scheduler は自分の broker に属する cron task だけを過不足なく発見する。"""
    discovered = await _discovered_cron_task_names(scheduler)
    assert discovered == expected


@pytest.mark.asyncio
async def test_all_cron_partitioned_across_schedulers() -> None:
    """5 scheduler 全体で全 cron を漏れ・重複なく分割発見する (totality)。"""
    discovered = [await _discovered_cron_task_names(s) for _, s, _ in _EXPECTED_CRON]
    union: set[str] = set().union(*discovered)
    # 重複なし: 各 scheduler の発見数の総和が union サイズと一致 = pairwise disjoint
    # (同一 cron が複数 scheduler から二重発火しない)。
    assert sum(len(d) for d in discovered) == len(union)
    # 漏れなし: 時刻表 SSoT の全 cron を覆う。
    assert len(union) == _TOTAL_CRON_COUNT


@pytest.mark.asyncio
async def test_schedulerless_brokers_have_no_cron() -> None:
    """scheduler を持たない broker に schedule 付き task が無い (orphan cron 検出)。

    content / analysis / embedding broker は scheduler を持たないため、ここに cron が
    紛れ込むと永久に発火しない。taskiq 自身の discovery (LabelScheduleSource) で
    schedule 付き task を数え、空であることを保証する。
    """
    orphans: dict[str, set[str]] = {}
    for label, broker in (
        ("content", broker_content),
        ("analysis", broker_analysis),
        ("embedding", broker_embedding),
    ):
        source = LabelScheduleSource(broker)
        await source.startup()
        names = {task.task_name for task in await source.get_schedules()}
        if names:
            orphans[label] = names
    assert orphans == {}
