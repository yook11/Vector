"""``TrendsRepository`` の compile 時 bindparam 衝突回避を構造的に固定するテスト。

``_mention_counts_subquery`` は ``get_mention_candidates`` で週と前週の
2 回呼ばれ、同じ outer query に組み込まれる。素朴な
``.bindparams(window_start=...)`` (kwarg 形式) は param 名が衝突して後者で
上書きされるため、``sa.bindparam(..., unique=True)`` を使って SQLAlchemy が
自動 suffix を付ける形にしている。

本テストは ``literal_binds`` で SQL をレンダリングし、週と前週の公開日時の境界が
**すべて** SQL 文字列に残ることを確認する。これにより bindparam 衝突に
よって片方の週の値だけが残る回帰を構造的に検出する。``get_mention_key_points``
/ ``get_co_mentions`` は subquery を再利用せず単一 bindparam セットのため
衝突リスクがなく、実行時の正しさは integration テストで検証する。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.insights.trend_discovery.domain.trend import TrendWeeks
from app.insights.trend_discovery.repository import TrendsRepository


def _render(stmt: object) -> str:
    return str(
        stmt.compile(  # type: ignore[union-attr]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


class TestBindparamUniqueness:
    """``_mention_counts_subquery`` の 2 回呼び出しで週の値が両方残ることを固定。"""

    def test_get_mention_candidates_renders_both_weeks(self) -> None:
        weeks = TrendWeeks(snapshot_date=date(2026, 4, 20))
        week_sub = TrendsRepository._mention_counts_subquery(
            category_id=1, week=weeks.week, label="week"
        )
        previous_week_sub = TrendsRepository._mention_counts_subquery(
            category_id=1, week=weeks.previous_week, label="previous_week"
        )
        stmt = (
            select(
                week_sub.c.display_name,
                previous_week_sub.c.cnt,
            )
            .select_from(week_sub)
            .outerjoin(
                previous_week_sub,
                previous_week_sub.c.match_key == week_sub.c.match_key,
            )
        )
        sql = _render(stmt)

        # 週 4/13〜4/19 と前週 4/6〜4/12 の JST 0時の境界 (UTC で前日 15時) が
        # すべて残ること。週の開始は前週の終わりと同じ値になる。
        assert "2026-04-19 15:00:00" in sql
        assert "2026-04-12 15:00:00" in sql
        assert "2026-04-05 15:00:00" in sql
