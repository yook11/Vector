"""全 taskiq cron schedule の SSoT。

各 task の ``@broker.task(schedule=...)`` は本ファイルから定数を import する。
ハードコードを禁じることで、minute 衝突確認と JST/UTC 換算を本ファイルの
時刻表 docstring 1 か所で完結させる。

時刻表 (UTC / JST 換算):

  cron               | UTC          | JST          | task
  -------------------|--------------|--------------|---------------------------------
  * * * * *          | 毎分         | 毎分         | dispatch_html_fetch_jobs
                     |              |              | sweep_expired_leases
  * * * * *          | 毎分         | 毎分         | sweep_deadline_exceeded_agent_runs
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

# 1 分間隔 — article_completion stage の DB 駆動 poll / lease sweep
CRON_HTML_FETCH = "* * * * *"

# 1 分間隔 — agent run の期限切れを確定
CRON_AGENT_RUN_SWEEP = "* * * * *"

# JST 毎日 00:05 — rolling 7d Trend Discovery 実行 (UTC 前日 15:05)
CRON_TREND_DISCOVERY = "5 15 * * *"

# JST 月曜 00:05 — 週次 briefing 生成 (UTC 日曜 15:05)
CRON_WEEKLY_BRIEFING = "5 15 * * 0"
