"""``ExtractionService`` の 3 Outcome 経路で audit が焼付けられる integration test
(PR3-a-1)。

検証する性質:
- ``ExtractedOutcome`` (signal) → ``outcome_code='extracted'`` (SUCCEEDED)
- ``NoiseOutcome`` (relevance=noise) → ``outcome_code='extracted_as_noise'``
  (SUCCEEDED)
- ``InvalidInputOutcome`` (InvalidInputError catch) →
  ``outcome_code='skipped_invalid_input'`` (SKIPPED)
- 各 audit row に ``ai_model`` / ``prompt_version`` / ``input_content_*`` /
  ``source_name`` が payload に焼かれている
- 成功系では ``ai_raw_response`` / ``entity_count`` も焼かれる
- ``article_id`` / ``source_id`` (auto-resolve) が両方埋まる
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.domain.value_objects.entity import EntityRawType, EntitySurface
from app.analysis.errors import InvalidInputError
from app.analysis.extraction.domain import ExtractedEntity, ExtractionResult
from app.analysis.extraction.domain.ready import ReadyForExtraction
from app.analysis.extraction.extractor.base import BaseExtractor
from app.analysis.extraction.extractor.envelope import ExtractionCall
from app.analysis.extraction.extractor.gemini_prompt import GeminiExtractionPrompt
from app.analysis.extraction.service import (
    ExtractedOutcome,
    ExtractionService,
    InvalidInputOutcome,
    NoiseOutcome,
)
from app.models.article import Article
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent


def _result(relevance: str = "signal", entities: int = 1) -> ExtractionResult:
    return ExtractionResult(
        relevance=relevance,
        title_ja="日本語タイトル",
        summary_ja="日本語要約",
        entities=[
            ExtractedEntity(
                surface=EntitySurface(f"E{i}"), raw_type=EntityRawType("Company")
            )
            for i in range(entities)
        ],
    )


def _envelope(
    relevance: str = "signal", *, raw: str = '{"relevance":"signal"}'
) -> ExtractionCall:
    return ExtractionCall(
        result=_result(relevance=relevance, entities=2),
        raw_response=raw,
        prompt_version=GeminiExtractionPrompt.VERSION,
    )


def _extractor(
    *, return_envelope: ExtractionCall | None = None, side_effect=None
) -> BaseExtractor:
    mock = MagicMock(spec=BaseExtractor)
    type(mock).model_name = GeminiExtractionPrompt.MODEL
    if side_effect is not None:
        mock.extract = AsyncMock(side_effect=side_effect)
    else:
        mock.extract = AsyncMock(return_value=return_envelope or _envelope())
    return mock


async def _make_article(
    db_session: AsyncSession, sample_source: NewsSource, url: str = "https://e.com/a"
) -> Article:
    article = Article(
        source_id=sample_source.id,
        source_url=url,  # type: ignore[arg-type]
        original_title="Original Title",
        original_content="content body x" * 50,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    return article


async def _ready(article: Article) -> ReadyForExtraction:
    return ReadyForExtraction(
        article_id=article.id,
        original_title=article.original_title,
        original_content=article.original_content,
    )


async def _fetch_extraction_events(
    db_session: AsyncSession, article_id: int
) -> list[PipelineEvent]:
    stmt = (
        select(PipelineEvent)
        .where(
            PipelineEvent.article_id == article_id,
            PipelineEvent.stage == "extraction",
        )
        .order_by(PipelineEvent.id)
    )
    return list((await db_session.execute(stmt)).scalars().all())


@pytest.mark.asyncio
async def test_signal_outcome_writes_extracted_audit(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    article = await _make_article(db_session, sample_source)
    ready = await _ready(article)
    svc = ExtractionService(session_factory)

    outcome = await svc.execute(ready, _extractor(return_envelope=_envelope("signal")))

    assert isinstance(outcome, ExtractedOutcome)
    events = await _fetch_extraction_events(db_session, article.id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == "extracted"
    assert ev.source_id == sample_source.id
    payload = ev.payload
    assert payload["ai_model"] == GeminiExtractionPrompt.MODEL
    assert payload["prompt_version"] == GeminiExtractionPrompt.VERSION
    assert payload["source_name"] == str(sample_source.name)
    assert payload["entity_count"] == 2
    assert payload["ai_raw_response"]
    assert payload["input_content_length"] == len(article.original_content)


@pytest.mark.asyncio
async def test_noise_outcome_writes_extracted_as_noise_audit(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    article = await _make_article(db_session, sample_source)
    ready = await _ready(article)
    svc = ExtractionService(session_factory)

    outcome = await svc.execute(ready, _extractor(return_envelope=_envelope("noise")))

    assert isinstance(outcome, NoiseOutcome)
    events = await _fetch_extraction_events(db_session, article.id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == "extracted_as_noise"


@pytest.mark.asyncio
async def test_invalid_input_outcome_writes_skipped_audit(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    article = await _make_article(db_session, sample_source)
    ready = await _ready(article)
    svc = ExtractionService(session_factory)

    outcome = await svc.execute(
        ready, _extractor(side_effect=InvalidInputError("malformed input"))
    )

    assert isinstance(outcome, InvalidInputOutcome)
    events = await _fetch_extraction_events(db_session, article.id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "skipped"
    assert ev.outcome_code == "skipped_invalid_input"
    assert ev.error_class is not None
    assert "InvalidInputError" in ev.error_class
    payload = ev.payload
    assert payload["error_message"] is not None
    assert payload["error_chain"] is not None
    # 失敗系でも 6 共通 field は populate される
    assert payload["ai_model"] == GeminiExtractionPrompt.MODEL
    assert payload["input_content_length"] == len(article.original_content)
