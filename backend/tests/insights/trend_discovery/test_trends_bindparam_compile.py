"""``TrendsRepository`` の compile 時 bindparam 衝突回避を構造的に固定するテスト。

``_entity_window_subquery`` は ``get_ranked_mentions`` で current_sub と
previous_sub の 2 回呼ばれ、同じ outer query に組み込まれる。素朴な
``.bindparams(window_start=...)`` (kwarg 形式) は param 名が衝突して後者で
上書きされるため、``sa.bindparam(..., unique=True)`` を使って SQLAlchemy が
自動 suffix を付ける形にしている。

本テストは ``literal_binds`` で SQL をレンダリングし、current / previous 両 window
の値が **すべて** SQL 文字列に残ることを確認する。これにより bindparam 衝突に
よって片方の window 値だけが残る回帰を構造的に検出する。``get_mention_key_points``
/ ``get_related_mentions`` は subquery を再利用せず単一 bindparam セットのため
衝突リスクがなく、実行時の正しさは integration テストで検証する。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.insights.trend_discovery.repository import TrendsRepository

JST = ZoneInfo("Asia/Tokyo")


def _jst(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 0, tzinfo=JST)


def _render(stmt: object) -> str:
    return str(
        stmt.compile(  # type: ignore[union-attr]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


class TestBindparamUniqueness:
    """``_entity_window_subquery`` の 2 回呼び出しで window 値が両方残ることを固定。"""

    def test_get_ranked_mentions_renders_all_windows(self) -> None:
        current_start = _jst(2026, 4, 13)
        current_end = _jst(2026, 4, 20)
        previous_start = _jst(2026, 4, 6)

        current_sub = TrendsRepository._entity_window_subquery(
            category_id=1,
            window_start=current_start,
            window_end=current_end,
            label="current",
        )
        previous_sub = TrendsRepository._entity_window_subquery(
            category_id=1,
            window_start=previous_start,
            window_end=current_start,
            label="previous",
        )
        stmt = (
            select(
                current_sub.c.display_name,
                previous_sub.c.cnt,
            )
            .select_from(current_sub)
            .outerjoin(
                previous_sub,
                previous_sub.c.match_key == current_sub.c.match_key,
            )
        )
        sql = _render(stmt)

        # current の window_start (2026-04-13) / window_end (2026-04-20)、
        # previous の window_start (2026-04-06) の 3 値すべてが残ること。
        assert "2026-04-13" in sql
        assert "2026-04-20" in sql
        assert "2026-04-06" in sql
