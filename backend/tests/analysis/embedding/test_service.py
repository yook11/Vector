"""実DBで保存・生成済み・コミット失敗時のspanと成功カウンタを検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from logfire.testing import CaptureLogfire
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.domain.value_objects import (
    EMBEDDING_DIMENSION,
    EmbeddingVector,
)
from app.analysis.embedding.errors import EmbeddingAnalyzedArticleMissingError
from app.analysis.embedding.service import EmbeddingService
from app.logfire.article_stage import embedding_stage_span
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.category import Category
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent
from tests.logfire._metric_helpers import collected_metrics, sum_counter_for_result
from tests.logfire._span_helpers import stage_attrs

_METRIC = "vector.embedding.processing_outcome"
_ALL_RESULTS = ("succeeded", "failed", "infra_error")


def _make_embedder() -> MagicMock:
    fake = MagicMock(spec=BaseEmbedder)
    fake.model_name = "gemini-embedding-001"
    fake.dimension = EMBEDDING_DIMENSION
    fake.embed_document = AsyncMock(
        return_value=EmbeddingVector(root=tuple([0.1] * EMBEDDING_DIMENSION))
    )
    return fake


@pytest.fixture
async def embedding_article(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> tuple[AnalyzedArticleRecord, int]:
    article = AnalyzableArticleRecord(
        source_id=sample_source.id,
        source_url="https://example.com/embedding-observation",
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
    analysis = AnalyzedArticleRecord(
        curation_id=curation.id,
        translated_title="title",
        summary="summary",
        investor_take="take",
        category_id=sample_categories[0].id,
    )
    db_session.add(analysis)
    await db_session.commit()
    return analysis, article.id


async def _execute(
    session_factory: async_sessionmaker[AsyncSession],
    analysis_id: int,
    article_id: int,
) -> None:
    ready = ReadyForEmbedding(
        analyzed_article_id=analysis_id, text_for_embedding="summary"
    )
    with embedding_stage_span(analyzed_article_id=analysis_id):
        await EmbeddingService(session_factory).execute(
            ready,
            _make_embedder(),
            analyzable_article_id=article_id,
        )


@pytest.mark.asyncio
async def test_save_success_sets_stage_result_succeeded(
    session_factory: async_sessionmaker[AsyncSession],
    embedding_article: tuple[AnalyzedArticleRecord, int],
    capfire: CaptureLogfire,
) -> None:
    """保存と成功監査の確定後にsucceededを記録する。"""
    analysis, article_id = embedding_article
    await _execute(session_factory, analysis.id, article_id)
    assert stage_attrs(capfire)["result"] == "succeeded"


@pytest.mark.asyncio
async def test_save_success_emits_processing_outcome_succeeded(
    session_factory: async_sessionmaker[AsyncSession],
    embedding_article: tuple[AnalyzedArticleRecord, int],
    capfire: CaptureLogfire,
) -> None:
    """正常保存だけを成功カウンタに計上する。"""
    analysis, article_id = embedding_article
    await _execute(session_factory, analysis.id, article_id)
    metrics = collected_metrics(capfire)
    assert sum_counter_for_result(metrics, _METRIC, "succeeded") == 1
    for other in ("failed", "infra_error"):
        assert sum_counter_for_result(metrics, _METRIC, other) == 0


@pytest.mark.asyncio
async def test_race_loss_sets_stage_result_skipped(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    embedding_article: tuple[AnalyzedArticleRecord, int],
    capfire: CaptureLogfire,
) -> None:
    """保存前に生成済みを確認した場合はskippedを記録する。"""
    analysis, article_id = embedding_article
    analysis.embedding = [0.4] * EMBEDDING_DIMENSION
    await db_session.commit()
    await _execute(session_factory, analysis.id, article_id)
    assert stage_attrs(capfire)["result"] == "skipped"


@pytest.mark.asyncio
async def test_race_loss_does_not_emit_processing_outcome(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    embedding_article: tuple[AnalyzedArticleRecord, int],
    capfire: CaptureLogfire,
) -> None:
    """生成済みの正常終了は成功数や失敗数を増やさない。"""
    analysis, article_id = embedding_article
    analysis.embedding = [0.4] * EMBEDDING_DIMENSION
    await db_session.commit()
    await _execute(session_factory, analysis.id, article_id)
    for result in _ALL_RESULTS:
        assert sum_counter_for_result(collected_metrics(capfire), _METRIC, result) == 0


@pytest.mark.asyncio
async def test_commit_failure_does_not_emit_succeeded(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    embedding_article: tuple[AnalyzedArticleRecord, int],
    capfire: CaptureLogfire,
) -> None:
    """監査INSERTの主キー競合でcommitが失敗すると、ベクトルもロールバックする。"""
    analysis, article_id = embedding_article
    analysis_id = analysis.id
    # 明示IDでsequenceを進めず、Serviceのcommit時に実DBの制約違反を発生させる。
    db_session.add(
        PipelineEvent(
            id=1,
            stage="embedding",
            event_type="succeeded",
            outcome_code="existing_event",
            payload={},
        )
    )
    await db_session.commit()
    with pytest.raises(IntegrityError):
        await _execute(session_factory, analysis_id, article_id)
    db_session.expire_all()
    stored = await db_session.get(AnalyzedArticleRecord, analysis_id)
    assert stored is not None and stored.embedding is None
    assert len((await db_session.execute(select(PipelineEvent))).scalars().all()) == 1
    for result in _ALL_RESULTS:
        assert sum_counter_for_result(collected_metrics(capfire), _METRIC, result) == 0


@pytest.mark.asyncio
async def test_missing_article_is_not_observed_as_skipped_or_succeeded(
    session_factory: async_sessionmaker[AsyncSession],
    capfire: CaptureLogfire,
) -> None:
    """保存先不存在を正常な競合として観測しない。"""
    with pytest.raises(EmbeddingAnalyzedArticleMissingError):
        await _execute(session_factory, 999_999, 999_999)
    assert stage_attrs(capfire)["result"] not in ("skipped", "succeeded")
    assert sum_counter_for_result(collected_metrics(capfire), _METRIC, "succeeded") == 0
