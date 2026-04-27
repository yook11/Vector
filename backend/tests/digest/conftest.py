"""digest BC テスト固有のフィクスチャ。

repository / Service テスト向けの ``seed_analysis`` ファクトリを提供する。
seed_analysis は 1 件の ``ArticleAnalysis`` を関連 ORM (DiscoveredArticle /
Article / ArticleExtraction / ArticleEntity) とともに作成する。

URL の重複制約を避けるため fixture 内のカウンタで一意な URL を採番する
(関数スコープ fixture なのでテストごとにリセットされる)。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from itertools import count

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.article_analysis import ArticleAnalysis
from app.models.article_entity import ArticleEntity
from app.models.article_extraction import ArticleExtraction
from app.models.discovered_article import DiscoveredArticle
from app.models.news_source import NewsSource

SeedAnalysis = Callable[..., Awaitable[ArticleAnalysis]]


@pytest.fixture
def seed_analysis(db_session: AsyncSession, sample_source: NewsSource) -> SeedAnalysis:
    """1 件の ``ArticleAnalysis`` を関連 ORM ごと seed するファクトリ。

    Args (キーワード引数):
        category_id: ``ArticleAnalysis.category_id`` に設定する FK。
        analyzed_at: ``analyzed_at`` を明示指定 (server_default を上書き)。
        topic: ``ArticleAnalysis.topic`` (TopicName VO)。デフォルト ``"ai agents"``。
        entities: ``[(name, type), ...]`` の列。``ArticleEntity`` を生成する。

    Returns:
        永続化済みの ``ArticleAnalysis``。flush のみで commit はしない
        (呼び出し側のトランザクション境界に従う)。
    """
    seq = count()

    async def _seed(
        *,
        category_id: int,
        analyzed_at: datetime,
        topic: str = "ai agents",
        entities: Sequence[tuple[str, str]] = (),
    ) -> ArticleAnalysis:
        n = next(seq)
        discovered = DiscoveredArticle(
            news_source_id=sample_source.id,
            original_title=f"seed-{n}",
            original_url=f"https://example.com/seed-{n}",
        )
        db_session.add(discovered)
        await db_session.flush()

        article = Article(
            discovered_article_id=discovered.id,
            original_title=f"seed-{n}",
            original_content="x" * 60,
        )
        db_session.add(article)
        await db_session.flush()

        extraction = ArticleExtraction(
            article_id=article.id,
            translated_title=f"seed-{n}",
            summary="summary body",
            ai_model="test",
            entities=[ArticleEntity(name=name, type=type_) for name, type_ in entities],
        )
        db_session.add(extraction)
        await db_session.flush()

        analysis = ArticleAnalysis(
            extraction_id=extraction.id,
            translated_title=f"seed-{n}",
            summary="summary body",
            investor_take="investor take body",
            ai_model="test",
            topic=topic,
            category_id=category_id,
            analyzed_at=analyzed_at,
        )
        db_session.add(analysis)
        await db_session.flush()
        return analysis

    return _seed
