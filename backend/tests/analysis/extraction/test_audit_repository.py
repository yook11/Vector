"""``ExtractionAuditRepository`` の semantic method 単独テスト (PR3.5-c)。

audit row の shape SSoT が repository に集約されたことを検証する:

- ``append_extracted`` / ``append_noise`` で
  ``category=success`` + ``code`` (caller 渡し) / ``outcome_code=code`` が記録
- ``append_drop_article`` で
  ``category=non_retryable_drop_article`` + ``code=type(exc).CODE``
- ``append_failure`` で **exc 型による 4 dispatch** が動作:
  - ``NonRetryableDropArticle`` → ``category=non_retryable_drop_article``
  - ``NonRetryableKeepArticle`` → ``category=non_retryable_keep_article``
  - ``RetryableError`` → ``category=retryable``
  - 想定外 ``RuntimeError`` → ``category=unknown`` / ``code=unexpected_error``
- repository は ``commit`` を呼ばない (caller の tx 境界保持)
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
)
from app.analysis.extraction.ai.base import BaseExtractor
from app.analysis.extraction.ai.envelope import ExtractionCall
from app.analysis.extraction.ai.gemini_spec import GEMINI_EXTRACTION_SPEC
from app.analysis.extraction.audit_repository import ExtractionAuditRepository
from app.analysis.extraction.domain import Noise, Signal
from app.analysis.extraction.domain.ready import ReadyForExtraction
from app.analysis.extraction.errors import ExtractionResponseInvalidError
from app.models.article import Article
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent


def _extractor_mock(
    *,
    model: str = "test-extract-model",
    prompt_version: str = "test-extract-prompt-v1",
) -> MagicMock:
    """失敗 audit テスト用の ``BaseExtractor`` mock。

    PR4 で ``BaseExtractor`` の構造保証は property 契約 (model_name /
    prompt_version / rate_policy) に置き換わったため、property 属性として
    値を bind する。値は test-* で Gemini と衝突しない名前にする。
    """
    mock = MagicMock(spec=BaseExtractor)
    type(mock).model_name = model
    type(mock).prompt_version = prompt_version
    return mock


def _signal_envelope() -> ExtractionCall[Signal]:
    return ExtractionCall(
        result=Signal(title_ja="日本語タイトル", summary_ja="日本語要約"),
        raw_response='{"relevance":"signal"}',
        raw_relevance="signal",
        prompt_version=GEMINI_EXTRACTION_SPEC.version,
        model_name=GEMINI_EXTRACTION_SPEC.model,
    )


def _noise_envelope() -> ExtractionCall[Noise]:
    return ExtractionCall(
        result=Noise(title_ja="日本語タイトル", summary_ja="日本語要約"),
        raw_response='{"relevance":"noise"}',
        raw_relevance="noise",
        prompt_version=GEMINI_EXTRACTION_SPEC.version,
        model_name=GEMINI_EXTRACTION_SPEC.model,
    )


async def _make_article(
    db_session: AsyncSession, sample_source: NewsSource, *, content: str = "body x" * 30
) -> Article:
    article = Article(
        source_id=sample_source.id,
        source_url="https://e.com/a",  # type: ignore[arg-type]
        original_title="t",
        original_content=content,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    return article


def _ready(article: Article) -> ReadyForExtraction:
    return ReadyForExtraction(
        article_id=article.id,
        original_title=article.original_title,
        original_content=article.original_content,
    )


async def _fetch_one(db_session: AsyncSession, article_id: int) -> PipelineEvent:
    rows = (
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.article_id == article_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    return rows[0]


# ---------------------------------------------------------------------------
# 成功経路 — append_extracted / append_noise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_extracted_records_success_with_code(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """signal 経路で category=success / code=extracted が記録される。"""
    article = await _make_article(db_session, sample_source)
    async with session_factory() as session:
        await ExtractionAuditRepository(session).append_extracted(
            ready=_ready(article),
            envelope=_signal_envelope(),
            code="extracted",
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == "extracted"
    assert ev.category == "success"
    assert ev.code == "extracted"
    assert ev.payload["ai_raw_response"]
    assert ev.payload["source_name"] == str(sample_source.name)
    # PR1-a: ai_model / prompt_version / raw_relevance は envelope 経由で焼かれる
    assert ev.payload["ai_model"] == GEMINI_EXTRACTION_SPEC.model
    assert ev.payload["prompt_version"] == GEMINI_EXTRACTION_SPEC.version
    assert ev.payload["raw_relevance"] == "signal"


@pytest.mark.asyncio
async def test_append_noise_records_extracted_as_noise(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """noise 経路で code=extracted_as_noise が記録される。"""
    article = await _make_article(db_session, sample_source)
    async with session_factory() as session:
        await ExtractionAuditRepository(session).append_noise(
            ready=_ready(article),
            envelope=_noise_envelope(),
            code="extracted_as_noise",
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.outcome_code == "extracted_as_noise"
    assert ev.category == "success"
    assert ev.code == "extracted_as_noise"
    # PR1-a: raw_relevance は envelope.raw_relevance ("noise") から焼かれる
    assert ev.payload["raw_relevance"] == "noise"


# ---------------------------------------------------------------------------
# DROP 経路 — append_drop_article
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_drop_article_records_failure_with_drop_category(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """drop 経路で category=non_retryable_drop_article / code=type(exc).CODE が記録。"""
    article = await _make_article(db_session, sample_source)
    article_id = article.id
    exc = AIProviderOutputBlockedError("blocked by SAFETY")
    extractor = _extractor_mock()

    async with session_factory() as session:
        await ExtractionAuditRepository(session).append_drop_article(
            article_id=article_id,
            original_content=article.original_content,
            code=type(exc).CODE,
            exc=exc,
            extractor=extractor,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article_id)
    assert ev.event_type == "failed"
    assert ev.outcome_code == "ai_error_output_blocked"
    assert ev.category == "non_retryable_drop_article"
    assert ev.code == "ai_error_output_blocked"
    assert ev.error_class is not None
    assert ev.error_class.endswith(".AIProviderOutputBlockedError")
    assert ev.payload["error_message"] is not None
    assert ev.payload["error_chain"]
    assert ev.payload["error_chain"][0].endswith(".AIProviderOutputBlockedError")
    # PR2: 失敗 audit の ai_model / prompt_version は extractor 経由
    # (Gemini ClassVar hardcode を消した)
    assert ev.payload["ai_model"] == "test-extract-model"
    assert ev.payload["prompt_version"] == "test-extract-prompt-v1"


# ---------------------------------------------------------------------------
# 失敗経路 — append_failure (4 marker dispatch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc_factory", "expected_category", "expected_code"),
    [
        (
            lambda: AIProviderInputRejectedError("ctx too long"),
            "non_retryable_drop_article",
            "ai_error_input_rejected",
        ),
        (
            lambda: AIProviderConfigurationError("api key missing"),
            "non_retryable_keep_article",
            "ai_error_configuration",
        ),
        (
            lambda: AIProviderNetworkError("conn reset"),
            "retryable",
            "ai_error_network",
        ),
        (
            lambda: ExtractionResponseInvalidError("schema violation"),
            "retryable",
            "extraction_response_invalid",
        ),
        (lambda: RuntimeError("surprise"), "unknown", "unexpected_error"),
    ],
)
async def test_append_failure_dispatches_category_and_code_from_exc(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    exc_factory: object,
    expected_category: str,
    expected_code: str,
) -> None:
    """append_failure は exc 型から category/code を自動導出する。"""
    article = await _make_article(db_session, sample_source)
    exc = exc_factory()  # type: ignore[operator]
    extractor = _extractor_mock()

    async with session_factory() as session:
        await ExtractionAuditRepository(session).append_failure(
            ready=_ready(article),
            exc=exc,
            attempt=2,
            extractor=extractor,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.event_type == "failed"
    assert ev.category == expected_category
    assert ev.code == expected_code
    assert ev.outcome_code == expected_code  # Phase A: outcome_code = code
    assert ev.attempt == 2
    assert ev.error_class is not None
    assert ev.error_class.endswith(f".{type(exc).__name__}")
    # PR2: 失敗 audit の ai_model / prompt_version は extractor 経由
    # (Gemini ClassVar hardcode を消した)
    assert ev.payload["ai_model"] == "test-extract-model"
    assert ev.payload["prompt_version"] == "test-extract-prompt-v1"


# ---------------------------------------------------------------------------
# tx 境界 — repository は commit しない
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repository_does_not_commit(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """repository が caller commit を奪わないことを確認する。"""
    article = await _make_article(db_session, sample_source)

    async with session_factory() as session:
        await ExtractionAuditRepository(session).append_extracted(
            ready=_ready(article),
            envelope=_signal_envelope(),
            code="extracted",
        )
        # 意図的に commit しない (rollback で消える)

    rows = (
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.article_id == article.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 0  # 未 commit のため永続化されていない
