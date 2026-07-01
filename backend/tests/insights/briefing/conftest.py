"""briefing BC テスト固有のフィクスチャ。

snapshot 側 ``tests/insights/snapshot/conftest.py`` と同型の seed ファクトリ。
別 BC で要件 (briefing は ``translated_title`` と ``summary`` を JOIN するだけ
で entities は不要) が異なるため明示的に分けている。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from itertools import count
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.news_source import NewsSource

SeedBriefingAnalysis = Callable[..., Awaitable[AnalyzedArticleRecord]]


@pytest.fixture
def seed_briefing_analysis(
    db_session: AsyncSession, sample_source: NewsSource
) -> SeedBriefingAnalysis:
    """1 件の analysis (translated_title + summary 付き) を関連 ORM ごと seed する。

    Briefing repository が JOIN する article record / curation / assessment を
    最小限作る。
    """
    seq = count()

    async def _seed(
        *,
        category_id: int,
        analyzed_at: datetime,
        translated_title: str | None = None,
        summary: str | None = None,
        published_at: datetime | None = None,
        key_points: list[dict[str, Any]] | None = None,
    ) -> AnalyzedArticleRecord:
        n = next(seq)
        title = translated_title or f"briefing-seed-{n}"
        body = summary or f"briefing summary {n}"
        url = f"https://example.com/briefing-seed-{n}"

        article = AnalyzableArticleRecord(
            source_id=sample_source.id,
            source_url=url,
            original_title=title,
            original_content="x" * 60,
            # 未指定は analyzed_at に倒す (published_at は DB NOT NULL)。
            published_at=published_at if published_at is not None else analyzed_at,
        )
        db_session.add(article)
        await db_session.flush()

        extraction = ArticleCuration(
            analyzable_article_id=article.id,
            translated_title=title,
            summary=body,
        )
        db_session.add(extraction)
        await db_session.flush()

        analysis = AnalyzedArticleRecord(
            curation_id=extraction.id,
            translated_title=title,
            summary=body,
            investor_take="investor take",
            category_id=category_id,
            analyzed_at=analyzed_at,
            key_points=key_points,
        )
        db_session.add(analysis)
        await db_session.flush()
        return analysis

    return _seed
