"""トレンドの開始判定に使う事実の取得を実DBで確認する。"""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import event

from app.insights.trend_discovery.domain.ready import TrendDiscoveryReadyBuildFacts
from app.insights.trend_discovery.domain.trend import TrendWindow
from app.insights.trend_discovery.repository import TrendsRepository
from app.models.trends_snapshot import TrendsSnapshot

WINDOW = TrendWindow(window_end=date(2026, 5, 3))


@pytest.mark.asyncio
async def test_loads_publication_count_even_when_analysis_is_late(
    db_session, sample_categories, seed_analysis
):
    """開始判定の記事数は分析日時によらず対象の公開期間で数える。"""
    for published_at in (
        datetime(2026, 4, 25, 15, tzinfo=UTC),
        datetime(2026, 4, 28, 15, tzinfo=UTC),
        datetime(2026, 5, 2, 15, tzinfo=UTC),
    ):
        await seed_analysis(
            category_id=sample_categories[0].id,
            published_at=published_at,
            analyzed_at=datetime(2026, 5, 4, tzinfo=UTC),
        )
    facts = await TrendsRepository(db_session).load_ready_build_facts(window=WINDOW)
    assert facts == TrendDiscoveryReadyBuildFacts(
        already_generated=False, source_analysis_count=2
    )


@pytest.mark.asyncio
async def test_loads_zero_articles_for_an_empty_ungenerated_window(db_session):
    """未生成で対象記事が存在しないことを件数0として返す。"""
    facts = await TrendsRepository(db_session).load_ready_build_facts(window=WINDOW)
    assert facts == TrendDiscoveryReadyBuildFacts(
        already_generated=False, source_analysis_count=0
    )


@pytest.mark.asyncio
async def test_existing_snapshot_skips_article_count_query(db_session):
    """保存済み期間は存在確認だけで終了し、記事の集計問い合わせをしない。"""
    db_session.add(
        TrendsSnapshot(
            window_end=WINDOW.window_end,
            bundle={"marker": "existing"},
            source_analysis_count=1,
            generated_at=datetime(2026, 5, 3, tzinfo=UTC),
        )
    )
    await db_session.flush()
    statements = []

    def capture(state):
        statements.append(state.statement)

    event.listen(db_session.sync_session, "do_orm_execute", capture)
    try:
        facts = await TrendsRepository(db_session).load_ready_build_facts(window=WINDOW)
    finally:
        event.remove(db_session.sync_session, "do_orm_execute", capture)
    assert facts == TrendDiscoveryReadyBuildFacts(
        already_generated=True, source_analysis_count=None
    )
    assert len(statements) == 1
    assert "analyzed_articles" not in str(statements[0])
