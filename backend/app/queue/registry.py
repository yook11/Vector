"""scheduler が cron 駆動 task を発見するための副作用 import 集約点。

scheduler entrypoint (``app.queue.scheduler_entrypoint``) は本 module を import し、
各 cron 駆動 task の `@broker.task(schedule=...)` を import 時に登録させる
(``broker.get_all_tasks()`` を満たし LabelScheduleSource が schedule label を回収できる
状態にする)。worker entrypoint は task module を直接引数として渡すため本 module を
通らない。
"""

from __future__ import annotations

# cron 駆動 task を含む module を列挙する。
# scheduler の LabelScheduleSource が `@broker.task(schedule=...)` label を回収する
# ため、本 module を import するだけで cron 登録が完了する。
import app.queue.tasks.acquisition  # noqa: F401  (dispatch_high/medium/low)
import app.queue.tasks.agent_run  # noqa: F401  (sweep_stale_agent_runs)
import app.queue.tasks.backfill  # noqa: F401  (backfill_curations/assessments/embeddings)
import app.queue.tasks.briefing  # noqa: F401  (dispatch_weekly_briefings)
import app.queue.tasks.completion  # noqa: F401  (dispatch_html_fetch_jobs, sweep_expired_leases)
import app.queue.tasks.queue_health  # noqa: F401  (observe_pipeline_queue_health)
import app.queue.tasks.retention  # noqa: F401  (purge_pipeline_events)
import app.queue.tasks.trend_discovery  # noqa: F401  (run_trend_discovery)
