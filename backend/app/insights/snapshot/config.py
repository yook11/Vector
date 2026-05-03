"""Digest BC の集計しきい値・期間定数。

ここで定義した値は ``WeeklyTrendsBundle`` の生成 (集計 SQL + Snapshot Service)
で共有される集計しきい値。

数値は Phase 1A の確定設計 (``project_weekly_digest_phase1a_design.md`` /
``plans/drafts/20260426-095922/PLAN.md``) より。

- ``MIN_CURRENT``: hot 判定の現週最低件数 (これ未満は noise として除外)
- ``MIN_PREVIOUS``: hot 判定の前週最低件数 (継続トレンド側の条件)
- ``NEW_BURST_THRESHOLD``: 前週 0 でも現週がこの件数以上なら burst として hot
- ``SMOOTHING``: hotness_score の分母 smoothing (前週 0 除算回避 + 過大評価防止)
- ``WEEK_TZ``: 週の境界 (月曜 00:00) を切る基準タイムゾーン
- ``NEW_ENTITY_LOOKBACK_WEEKS``: 「初出」判定で過去何週ロックバックするか

集約サイズの構造的上限 (``MAX_TRENDS_PER_CATEGORY`` 等) は VO 不変条件で
あり性質が違うため ``domain/trend.py`` 側に置く。
"""

from __future__ import annotations

MIN_CURRENT: int = 5
MIN_PREVIOUS: int = 2
NEW_BURST_THRESHOLD: int = 10
SMOOTHING: int = 2
WEEK_TZ: str = "Asia/Tokyo"
NEW_ENTITY_LOOKBACK_WEEKS: int = 4
