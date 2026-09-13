"""Consumerと旧Taskiqの接続テストで分析済み記事を準備する。"""

from datetime import UTC, datetime

import pytest

from app.analysis.assessment.events import ArticleAssessedInScope
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration


@pytest.fixture
async def embedding_target(db_session, sample_source, sample_categories):
    article = AnalyzableArticleRecord(
        source_id=sample_source.id,
        source_url="https://example.com/consumer",
        original_title="title",
        original_content="content",
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.flush()
    curation = ArticleCuration(
        analyzable_article_id=article.id, translated_title="title", summary="summary"
    )
    db_session.add(curation)
    await db_session.flush()
    analyzed = AnalyzedArticleRecord(
        curation_id=curation.id,
        translated_title="title",
        summary="summary",
        investor_take="take",
        category_id=sample_categories[0].id,
    )
    db_session.add(analyzed)
    await db_session.commit()
    return ArticleAssessedInScope(
        curation_id=curation.id, analyzed_article_id=analyzed.id
    ), article.id
