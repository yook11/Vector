"""全 taskiq cron schedule の SSoT。

各 task の ``@broker.task(schedule=...)`` は本ファイルから定数を import する。
ハードコードを禁じることで、minute 衝突確認と JST/UTC 換算を本ファイルの
時刻表 docstring 1 か所で完結させる。

時刻表 (UTC / JST 換算):

  cron               | UTC          | JST          | task
  -------------------|--------------|--------------|---------------------------------
  * * * * *          | 毎分         | 毎分         | dispatch_html_fetch_jobs
                     |              |              | sweep_expired_leases
                     |              |              | observe_pipeline_queue_health
  */15 * * * *       | :00,:15,...  | :00,:15,...  | dispatch_high
  0,30 * * * *       | :00,:30      | :00,:30      | backfill_curations
  5,35 * * * *       | :05,:35      | :05,:35      | backfill_assessments
  10,40 * * * *      | :10,:40      | :10,:40      | backfill_embeddings
  20,50 * * * *      | :20,:50      | :20,:50      | purge_auth_rate_limits
  3,13,23,33,43,53 * * * * | :03,:13,... | :03,:13,... | sweep_stale_agent_runs
  25 * * * *         | :25          | :25          | purge_pipeline_events
  0 * * * *          | :00          | :00          | dispatch_medium
  0 */6 * * *        | 00,06,12,18  | (UTC=JST-9)  | dispatch_low
  5 15 * * *         | 15:05        | 00:05 (毎日) | run_trend_discovery
  5 15 * * 0         | Sun 15:05    | Mon 00:05    | dispatch_weekly_briefings

minute 衝突確認は本表で行う (新規 cron 追加時の overlap 回避 SSoT)。
新規 cron を増やすときは:
  1. 本表に行を追加 (UTC / JST 換算を併記)
  2. `CRON_*` 定数を追加
  3. task 側で `from app.queue.schedule import CRON_XXX` し
     `@broker.task(schedule=[{"cron": CRON_XXX}])` で参照する
"""

from __future__ import annotations

from app.collection.sources.fetch_cadence import FetchCadence

# 1 分間隔 — article_completion stage の DB 駆動 poll / lease sweep
CRON_HTML_FETCH = "* * * * *"

# 1 分間隔 — curation / assessment Redis Stream health の継続観測
CRON_PIPELINE_QUEUE_HEALTH = "* * * * *"

# 30 分間隔 — curation back-fill (Stage 3 救済、:00 / :30 起動)
CRON_BACKFILL_CURATIONS = "0,30 * * * *"

# 30 分間隔 + 5 分オフセット — assessment back-fill (Stage 4 救済、:05 / :35 起動)
CRON_BACKFILL_ASSESSMENTS = "5,35 * * * *"

# 30 分間隔 + 10 分オフセット — embedding back-fill (Stage 5 救済、:10 / :40 起動)
CRON_BACKFILL_EMBEDDINGS = "10,40 * * * *"

# 30 分間隔 + 20 分オフセット — Better Auth rateLimit retention purge
CRON_AUTH_RATE_LIMIT_PURGE = "20,50 * * * *"

# 10 分間隔 + 3 分オフセット — agent run stale 回収
CRON_AGENT_RUN_SWEEP = "3,13,23,33,43,53 * * * *"

# :25 — pipeline_events retention purge (他 cron と最少 overlap な minute)
CRON_PIPELINE_EVENTS_PURGE = "25 * * * *"

# JST 毎日 00:05 — rolling 7d Trend Discovery 実行 (UTC 前日 15:05)
CRON_TREND_DISCOVERY = "5 15 * * *"

# JST 月曜 00:05 — 週次 briefing 生成 (UTC 日曜 15:05)
CRON_WEEKLY_BRIEFING = "5 15 * * 0"


# FetchCadence tier → cron 写像 (旧 brokers.CADENCE_CRON)。
# tier 別に固定間隔で dispatch する。調整は本 dict の変更 + scheduler restart のみで
# 可逆 (env / DB を経由しない)。
CADENCE_CRON: dict[FetchCadence, str] = {
    FetchCadence.HIGH: "*/15 * * * *",  # 15 分間隔
    FetchCadence.MEDIUM: "0 * * * *",  # 1 時間間隔
    FetchCadence.LOW: "0 */6 * * *",  # 6 時間間隔
}
