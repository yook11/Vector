"""briefing BC テスト固有のフィクスチャ。

snapshot 側 ``tests/insights/snapshot/conftest.py`` と同型の seed ファクトリ。
別 BC で要件 (briefing は ``translated_title`` と ``summary`` を JOIN するだけ
で entities は不要) が異なるため明示的に分けている。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from itertools import count

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.article_analysis import ArticleAnalysis
from app.models.article_extraction import ArticleExtraction
from app.models.news_source import NewsSource

SeedBriefingAnalysis = Callable[..., Awaitable[ArticleAnalysis]]


@pytest.fixture
def seed_briefing_analysis(
    db_session: AsyncSession, sample_source: NewsSource
) -> SeedBriefingAnalysis:
    """1 件の analysis (translated_title + summary 付き) を関連 ORM ごと seed する。

    Briefing repository が JOIN する Article / ArticleExtraction / ArticleAnalysis を
    最小限作る。``translated_title`` / ``summary`` を引数で上書きできる。
    """
    seq = count()

    async def _seed(
        *,
        category_id: int,
        analyzed_at: datetime,
        translated_title: str | None = None,
        summary: str | None = None,
    ) -> ArticleAnalysis:
        n = next(seq)
        title = translated_title or f"briefing-seed-{n}"
        body = summary or f"briefing summary {n}"
        url = f"https://example.com/briefing-seed-{n}"

        article = Article(
            source_id=sample_source.id,
            source_url=url,
            original_title=title,
            original_content="x" * 60,
        )
        db_session.add(article)
        await db_session.flush()

        extraction = ArticleExtraction(
            article_id=article.id,
            translated_title=title,
            summary=body,
            ai_model="test",
        )
        db_session.add(extraction)
        await db_session.flush()

        analysis = ArticleAnalysis(
            extraction_id=extraction.id,
            translated_title=title,
            summary=body,
            investor_take="investor take",
            ai_model="test",
            topic="ai agents",
            category_id=category_id,
            analyzed_at=analyzed_at,
        )
        db_session.add(analysis)
        await db_session.flush()
        return analysis

    return _seed
