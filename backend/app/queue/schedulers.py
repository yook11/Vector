"""共通catalogに残るcron brokerごとのTaskiqScheduler定義。

  - scheduler_dispatch:        収集 dispatch 用 cron
  - scheduler_briefing:        週次 briefing 用 cron
  - scheduler_agent:           agent run deadline sweeper 用 cron
  - scheduler_maintenance:     back-fill 救済 + retention purge 用 cron

本moduleの4つとTrend Discovery側の1つは、``app.queue.scheduler_entrypoint`` が
1プロセスで並行実行する。各schedulerは自分のbrokerへkickするため、task→queue
routingは不変。共通catalog側のcron task登録は``registry.py``を参照する。
"""

from __future__ import annotations

from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource

from app.queue.brokers import (
    broker_agent,
    broker_briefing,
    broker_dispatch,
    broker_maintenance,
)

scheduler_dispatch = TaskiqScheduler(
    broker=broker_dispatch,
    sources=[LabelScheduleSource(broker_dispatch)],
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
