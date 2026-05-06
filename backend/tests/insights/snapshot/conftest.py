"""digest BC テスト固有のフィクスチャ。

repository / Service テスト向けの ``seed_analysis`` ファクトリを提供する。
seed_analysis は 1 件の ``ArticleAnalysis`` を関連 ORM (ArticleUrl /
Article / ArticleExtraction / ArticleExtractionEntity) とともに作成する。

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
from app.models.article_extraction import ArticleExtraction
from app.models.article_extraction_entity import ArticleExtractionEntity
from app.models.news_source import NewsSource
from tests.factories.article_url import create_article_url

SeedAnalysis = Callable[..., Awaitable[ArticleAnalysis]]


@pytest.fixture
def seed_analysis(db_session: AsyncSession, sample_source: NewsSource) -> SeedAnalysis:
    """1 件の ``ArticleAnalysis`` を関連 ORM ごと seed するファクトリ。

    Args (キーワード引数):
        category_id: ``ArticleAnalysis.category_id`` に設定する FK。
        analyzed_at: ``analyzed_at`` を明示指定 (server_default を上書き)。
        topic: ``ArticleAnalysis.topic`` (TopicName VO)。デフォルト ``"ai agents"``。
        entities: ``[(surface, raw_type), ...]`` の列。``ArticleExtractionEntity``
            を生成する。``position`` は引数の出現順 (0-based) で自動採番される。

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
        url = f"https://example.com/seed-{n}"
        article_url = await create_article_url(
            db_session, source=sample_source, url=url
        )

        article = Article(
            article_url_id=article_url.id,
            source_id=sample_source.id,
            source_url=url,
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
            entities=[
                ArticleExtractionEntity(
                    surface=surface,
                    raw_type=raw_type,
                    position=i,
                )
                for i, (surface, raw_type) in enumerate(entities)
            ],
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
