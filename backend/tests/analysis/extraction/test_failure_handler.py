"""``ExtractionFailureHandler`` の integration test。

検証する性質 (Drop 経路):
- 1 tx 内で audit INSERT → article DELETE が両方完了する
- 順序: audit が先 (source_id 自動補完が article 健在時に確定)
- DELETE 後、``articles`` から該当 row が消える
- ``pipeline_events.article_id`` は ``ondelete=SET NULL`` のため audit 行は残る
  ただし新規 INSERT 時点では ``article_id`` が埋まっている (DELETE 前)
- ``source_id`` が auto-resolve される (article DELETE 後でも source 追跡可能)
- ``source_name`` が payload に保存される (FK 切断耐性)
- ``NonRetryableDropArticle`` 派生例外
  (``AIProviderOutputBlockedError`` / ``AIProviderInputRejectedError``) で
  ``category='non_retryable_drop_article'`` / ``code=type(exc).CODE`` /
  ``outcome_code=code`` (Phase A 同値) が記録される
- 戻り値 ``False`` (Drop 経路は taskiq retry させない)
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.ai_provider_errors import (
    AIProviderInputRejectedError,
    AIProviderOutputBlockedError,
)
from app.analysis.extraction.ai.base import BaseExtractor
from app.analysis.extraction.ai.gemini_spec import GEMINI_EXTRACTION_SPEC
from app.analysis.extraction.domain.ready import ReadyForExtraction
from app.analysis.extraction.failure_handling import ExtractionFailureHandler
from app.models.article import Article
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent


def _extractor_mock() -> MagicMock:
    """Handler に渡す ``BaseExtractor`` mock (model_name / prompt_version のみ)。"""
    mock = MagicMock(spec=BaseExtractor)
    type(mock).model_name = GEMINI_EXTRACTION_SPEC.model
    type(mock).prompt_version = GEMINI_EXTRACTION_SPEC.version
    return mock


async def _make_article(
    db_session: AsyncSession,
    sample_source: NewsSource,
    *,
    url: str = "https://e.com/a",
    content: str = "body content " * 30,
) -> Article:
    article = Article(
        source_id=sample_source.id,
        source_url=url,  # type: ignore[arg-type]
        original_title="t",
        original_content=content,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    return article


def _ready_from(article: Article) -> ReadyForExtraction:
    return ReadyForExtraction(
        article_id=article.id,
        original_title=article.original_title,
        original_content=article.original_content,
    )


@pytest.mark.asyncio
async def test_output_blocked_writes_audit_then_deletes_article(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """AIProviderOutputBlockedError 経路で category/code が正しく記録される。"""
    article = await _make_article(db_session, sample_source)
    article_id = article.id
    # rollback 後の expired-attr lazy reload を避けるため事前に値を取り出す
    expected_source_id = sample_source.id
    expected_source_name = str(sample_source.name)
    ready = _ready_from(article)
    handler = ExtractionFailureHandler(session_factory)

    exc = AIProviderOutputBlockedError("blocked by policy: SAFETY")
    reraise = await handler.handle(
        ready=ready,
        exc=exc,
        extractor=_extractor_mock(),
        attempt=1,
        last_attempt=False,
    )

    assert reraise is False

    # commit が走った別 tx の DB 状態を確認するため fresh session で検証
    await db_session.rollback()
    article_row = await db_session.get(Article, article_id)
    assert article_row is None  # DELETE 済

    events = list(
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.stage == "extraction")
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "failed"
    assert ev.outcome_code == "ai_error_output_blocked"
    assert ev.category == "non_retryable_drop_article"
    assert ev.code == "ai_error_output_blocked"
    # SET NULL: article_id は NULL に
    assert ev.article_id is None
    # source_id は auto-resolve で埋まっている (DELETE 前に INSERT したため)
    assert ev.source_id == expected_source_id
    payload = ev.payload
    assert payload["source_name"] == expected_source_name
    assert payload["ai_model"] == GEMINI_EXTRACTION_SPEC.model
    assert payload["error_message"] is not None
    assert payload["error_chain"] is not None


@pytest.mark.asyncio
async def test_input_rejected_writes_audit_then_deletes_article(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """AIProviderInputRejectedError 経路 (context length 超過 etc) も同様に記録。"""
    article = await _make_article(db_session, sample_source)
    article_id = article.id
    ready = _ready_from(article)
    handler = ExtractionFailureHandler(session_factory)

    exc = AIProviderInputRejectedError("input exceeds context length")
    reraise = await handler.handle(
        ready=ready,
        exc=exc,
        extractor=_extractor_mock(),
        attempt=1,
        last_attempt=False,
    )

    assert reraise is False
    await db_session.rollback()
    assert (await db_session.get(Article, article_id)) is None
    events = list(
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.stage == "extraction")
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "failed"
    assert ev.outcome_code == "ai_error_input_rejected"
    assert ev.category == "non_retryable_drop_article"
    assert ev.code == "ai_error_input_rejected"
