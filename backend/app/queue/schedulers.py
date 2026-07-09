"""TaskiqScheduler 定義 — cron 駆動を持つ broker ごとに 1 つ (計 5)。

  - scheduler_metadata:        収集 dispatch 用 cron
  - scheduler_trend_discovery: Trend Discovery 用 cron
  - scheduler_briefing:        週次 briefing 用 cron
  - scheduler_agent:           agent run stale sweeper 用 cron
  - scheduler_maintenance:     back-fill 救済 + retention purge 用 cron

5 つは ``app.queue.scheduler_entrypoint`` が 1 プロセスで並行実行する
(``python -m app.queue.scheduler_entrypoint``)。各 scheduler は自分の broker へ kick
するため task→queue routing は不変 (Option B、routing 不変条件は
``tests/test_scheduler_routing.py`` が pin)。cron 表現は ``schedule.py`` の SSoT を、
cron 駆動 task の副作用 import は ``registry.py`` を参照。
"""

from __future__ import annotations

from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource

from app.queue.brokers import (
    broker_agent,
    broker_briefing,
    broker_maintenance,
    broker_metadata,
    broker_trend_discovery,
)

scheduler_metadata = TaskiqScheduler(
    broker=broker_metadata,
    sources=[LabelScheduleSource(broker_metadata)],
)
scheduler_trend_discovery = TaskiqScheduler(
    broker=broker_trend_discovery,
    sources=[LabelScheduleSource(broker_trend_discovery)],
)
scheduler_briefing = TaskiqScheduler(
    broker=broker_briefing,
    sources=[LabelScheduleSource(broker_briefing)],
)
scheduler_agent = TaskiqScheduler(
    broker=broker_agent,
    sources=[LabelScheduleSource(broker_agent)],
)
scheduler_maintenance = TaskiqScheduler(
    broker=broker_maintenance,
    sources=[LabelScheduleSource(broker_maintenance)],
)
