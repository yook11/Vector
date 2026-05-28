"""TaskiqScheduler 定義 — cron 駆動を持つ broker ごとに 1 つ。

scheduler entrypoint:
  - taskiq scheduler app.queue.schedulers:scheduler_metadata (back-fill 用 cron)
  - taskiq scheduler app.queue.schedulers:scheduler_trend_discovery
    (Trend Discovery 用 cron)
  - taskiq scheduler app.queue.schedulers:scheduler_briefing (週次 briefing 用 cron)

cron 表現自体は ``schedule.py`` の SSoT を、cron 駆動 task の副作用 import は
``registry.py`` を参照のこと。
"""

from __future__ import annotations

from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource

from app.queue.brokers import broker_briefing, broker_metadata, broker_trend_discovery

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
