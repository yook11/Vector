"""``ExtractionService`` の Outcome 経路で audit が焼付けられる integration test
(PR3.5-c)。

検証する性質:
- ``ExtractedOutcome`` (signal) → ``outcome_code='extracted'`` (SUCCEEDED) +
  ``category='success'`` + ``code='extracted'``
- ``NoiseOutcome`` (relevance=noise) → ``outcome_code='extracted_as_noise'``
  (SUCCEEDED) + ``category='success'`` + ``code='extracted_as_noise'``
- ``ExtractionResponseInvalidError`` (Layer 2-B) は Service が catch せず
  そのまま raise される (audit は task 層が焼く責務)
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
from app.analysis.errors import ExtractionResponseInvalidError
from app.analysis.extraction.ai.base import BaseExtractor
from app.analysis.extraction.ai.envelope import ExtractionCall
from app.analysis.extraction.ai.gemini_prompt import GeminiExtractionPrompt
from app.analysis.extraction.domain import ExtractedEntity, Noise, Signal
from app.analysis.extraction.domain.ready import ReadyForExtraction
from app.analysis.extraction.service import (
    ExtractedOutcome,
    ExtractionService,
    NoiseOutcome,
)
from app.models.article import Article
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent


def _signal_envelope(
    entities: int = 2, *, raw: str = '{"relevance":"signal"}'
) -> ExtractionCall[Signal]:
    return ExtractionCall(
        result=Signal(
            title_ja="日本語タイトル",
            summary_ja="日本語要約",
            entities=[
                ExtractedEntity(
                    surface=EntitySurface(f"E{i}"), raw_type=EntityRawType("Company")
                )
                for i in range(entities)
            ],
        ),
        raw_response=raw,
        raw_relevance="signal",
        prompt_version=GeminiExtractionPrompt.VERSION,
        model_name=GeminiExtractionPrompt.MODEL,
    )


def _noise_envelope(
    entities: int = 2, *, raw: str = '{"relevance":"noise"}'
) -> ExtractionCall[Noise]:
    return ExtractionCall(
        result=Noise(
            title_ja="日本語タイトル",
            summary_ja="日本語要約",
            entities=[
                ExtractedEntity(
                    surface=EntitySurface(f"E{i}"), raw_type=EntityRawType("Company")
                )
                for i in range(entities)
            ],
        ),
        raw_response=raw,
        raw_relevance="noise",
        prompt_version=GeminiExtractionPrompt.VERSION,
        model_name=GeminiExtractionPrompt.MODEL,
    )


def _extractor(
    *,
    return_envelope: ExtractionCall[Signal] | ExtractionCall[Noise] | None = None,
    side_effect=None,
) -> BaseExtractor:
    mock = MagicMock(spec=BaseExtractor)
    type(mock).model_name = GeminiExtractionPrompt.MODEL
    if side_effect is not None:
        mock.extract = AsyncMock(side_effect=side_effect)
    else:
        mock.extract = AsyncMock(return_value=return_envelope or _signal_envelope())
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
async def test_signal_outcome_writes_extracted_audit_with_category_and_code(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """signal Outcome 経路で category=success / code=extracted が焼かれる。"""
    article = await _make_article(db_session, sample_source)
    ready = await _ready(article)
    svc = ExtractionService(session_factory)

    outcome = await svc.execute(ready, _extractor(return_envelope=_signal_envelope()))

    assert isinstance(outcome, ExtractedOutcome)
    events = await _fetch_extraction_events(db_session, article.id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == "extracted"
    assert ev.category == "success"
    assert ev.code == "extracted"
    assert ev.source_id == sample_source.id
    payload = ev.payload
    assert payload["ai_model"] == GeminiExtractionPrompt.MODEL
    assert payload["prompt_version"] == GeminiExtractionPrompt.VERSION
    assert payload["source_name"] == str(sample_source.name)
    assert payload["entity_count"] == 2
    assert payload["ai_raw_response"]
    assert payload["input_content_length"] == len(article.original_content)
    # PR1-a: raw_relevance は envelope.raw_relevance から焼かれる (Stage 4 対称)
    assert payload["raw_relevance"] == "signal"


@pytest.mark.asyncio
async def test_noise_outcome_writes_extracted_as_noise_audit_with_category_and_code(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """noise Outcome 経路で category=success / code=extracted_as_noise が焼かれる。"""
    article = await _make_article(db_session, sample_source)
    ready = await _ready(article)
    svc = ExtractionService(session_factory)

    outcome = await svc.execute(ready, _extractor(return_envelope=_noise_envelope()))

    assert isinstance(outcome, NoiseOutcome)
    events = await _fetch_extraction_events(db_session, article.id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == "extracted_as_noise"
    assert ev.category == "success"
    assert ev.code == "extracted_as_noise"


@pytest.mark.asyncio
async def test_response_invalid_error_passes_through_without_service_audit(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Layer 2-B 例外は Service が catch せずそのまま raise する (Task 層責務)。"""
    article = await _make_article(db_session, sample_source)
    ready = await _ready(article)
    svc = ExtractionService(session_factory)

    with pytest.raises(ExtractionResponseInvalidError):
        await svc.execute(
            ready,
            _extractor(side_effect=ExtractionResponseInvalidError("schema violation")),
        )

    # Service は audit を焼かない (失敗経路は task 層 record_extraction_failure 責務)
    events = await _fetch_extraction_events(db_session, article.id)
    assert len(events) == 0
