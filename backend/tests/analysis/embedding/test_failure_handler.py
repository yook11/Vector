"""``EmbeddingFailureHandler`` の integration test (Stage 4 と同形)。

検証する性質:

- hold (stage 退避) は provider error の回復クラス (mode) から導出される:
  OPERATOR_ACTION_REQUIRED は即時 hold、CONDITION_BASED_RECOVERY は retry
  exhaustion (last_attempt) で hold、それ以外は hold しない。
- ``last_attempt`` flag で raise/return が分岐する (Recoverable / catch-all)。
- 失敗ごとに audit row が 1 行記録される (row の詳細 golden は
  ``test_audit_repository.py`` が所有)。
- audit Repository が raise しても task は落ちず ``embedding_failure_audit_dropped``
  にフォールバックし、secret prefix が log field から redact される。

marker は production と同じく ``to_embedding_error`` で provider error から構築する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from logfire.testing import CaptureLogfire
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderError,
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
    AIProviderServiceUnavailableError,
    AIProviderUsageLimitExhaustedError,
)
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.errors import (
    EmbeddingResponseInvalidError,
    to_embedding_error,
)
from app.analysis.embedding.failure_handling import EmbeddingFailureHandler
from app.analysis.gemini_error_translator import GeminiContentRejectionReason
from app.audit.stages.embedding import EmbeddingAuditRepository
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent
from tests.logfire._metric_helpers import collected_metrics, sum_counter_for_result

_METRIC = "vector.embedding.processing_outcome"
_ALL_RESULTS = ("succeeded", "failed", "infra_error")


async def _make_article(
    db_session: AsyncSession,
    sample_source: NewsSource,
    *,
    url: str = "https://e.com/a",
) -> AnalyzableArticleRecord:
    article = AnalyzableArticleRecord(
        source_id=sample_source.id,
        source_url=url,  # type: ignore[arg-type]
        original_title="t",
        original_content="c" * 100,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    return article


def _ready_for(*, analyzed_article_id: int = 1234) -> ReadyForEmbedding:
    return ReadyForEmbedding(
        analyzed_article_id=analyzed_article_id,
        text_for_embedding="分析タイトル\n分析要約",
    )


async def _fetch_embedding_events(
    db_session: AsyncSession, article_id: int
) -> list[PipelineEvent]:
    rows = (
        (
            await db_session.execute(
                select(PipelineEvent)
                .where(PipelineEvent.article_id == article_id)
                .where(PipelineEvent.stage == "embedding")
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


def _input_rejected() -> AIProviderInputRejectedError:
    return AIProviderInputRejectedError(reason=GeminiContentRejectionReason.SAFETY)


_HOLD_CASES: list[tuple[AIProviderError, bool, str | None]] = [
    (AIProviderConfigurationError(), False, "ai_error_configuration"),
    (AIProviderConfigurationError(), True, "ai_error_configuration"),
    (_input_rejected(), False, None),
    (_input_rejected(), True, None),
    (AIProviderUsageLimitExhaustedError(), False, None),
    (AIProviderUsageLimitExhaustedError(), True, "ai_error_usage_limit_exhausted"),
    (AIProviderNetworkError(), True, None),
    (AIProviderRateLimitedError(), True, None),
    (AIProviderServiceUnavailableError(), True, None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_exc,last_attempt,expected_hold", _HOLD_CASES)
async def test_hold_reason_derived_from_provider_mode(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    provider_exc: AIProviderError,
    last_attempt: bool,
    expected_hold: str | None,
) -> None:
    """stage hold は marker 型ではなく provider error の回復クラスから決まる。"""
    article = await _make_article(db_session, sample_source)
    ready = _ready_for()
    handler = EmbeddingFailureHandler(session_factory)
    exc = to_embedding_error(provider_exc)

    decision = await handler.handle(
        ready=ready,
        exc=exc,
        last_attempt=last_attempt,
        analyzable_article_id=article.id,
    )

    assert decision.stage_hold_reason == expected_hold


@pytest.mark.asyncio
async def test_recoverable_with_retry_budget_returns_true(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Recoverable + retry 余地あり → taskiq retry に委ねる (reraise=True)。"""
    article = await _make_article(db_session, sample_source)
    ready = _ready_for()
    handler = EmbeddingFailureHandler(session_factory)

    exc = to_embedding_error(AIProviderNetworkError())
    decision = await handler.handle(
        ready=ready, exc=exc, last_attempt=False, analyzable_article_id=article.id
    )

    assert decision.reraise is True
    assert decision.stage_hold_reason is None


@pytest.mark.asyncio
async def test_recoverable_last_attempt_returns_false(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Recoverable + retry 上限到達 → reraise=False。"""
    article = await _make_article(db_session, sample_source)
    ready = _ready_for()
    handler = EmbeddingFailureHandler(session_factory)

    exc = to_embedding_error(AIProviderNetworkError())
    decision = await handler.handle(
        ready=ready, exc=exc, last_attempt=True, analyzable_article_id=article.id
    )

    assert decision.reraise is False


@pytest.mark.asyncio
async def test_terminal_returns_false_without_reraise(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Terminal は retry 余地に関わらず reraise=False。"""
    article = await _make_article(db_session, sample_source)
    ready = _ready_for()
    handler = EmbeddingFailureHandler(session_factory)

    exc = to_embedding_error(AIProviderConfigurationError())
    decision = await handler.handle(
        ready=ready, exc=exc, last_attempt=False, analyzable_article_id=article.id
    )

    assert decision.reraise is False


@pytest.mark.asyncio
async def test_terminal_writes_single_failure_audit_row(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Terminal で failure audit row が 1 行記録され原因軸が焼かれる。"""
    article = await _make_article(db_session, sample_source)
    article_id = article.id
    ready = _ready_for()
    handler = EmbeddingFailureHandler(session_factory)

    exc = to_embedding_error(_input_rejected())
    await handler.handle(
        ready=ready, exc=exc, last_attempt=False, analyzable_article_id=article_id
    )

    await db_session.rollback()
    events = await _fetch_embedding_events(db_session, article_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "failed"
    assert ev.outcome_code == "ai_error_input_rejected"
    assert ev.retryability == "non_retryable"
    assert ev.payload["failure_kind"] == "target_rejected"
    assert ev.payload["failure_reason"] == "safety"


@pytest.mark.asyncio
async def test_unexpected_with_retry_budget_returns_true(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """catch-all + retry 余地あり → unknown audit + reraise=True。"""
    article = await _make_article(db_session, sample_source)
    article_id = article.id
    ready = _ready_for()
    handler = EmbeddingFailureHandler(session_factory)

    decision = await handler.handle(
        ready=ready,
        exc=ValueError("surprise"),
        last_attempt=False,
        analyzable_article_id=article_id,
    )

    assert decision.reraise is True
    assert decision.stage_hold_reason is None
    await db_session.rollback()
    events = await _fetch_embedding_events(db_session, article_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.outcome_code == "unexpected_error"
    assert ev.retryability == "unknown"
    assert ev.payload["failure_kind"] == "unknown"


@pytest.mark.asyncio
async def test_unexpected_last_attempt_returns_false(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """catch-all + retry 上限到達 → reraise=False。"""
    article = await _make_article(db_session, sample_source)
    ready = _ready_for()
    handler = EmbeddingFailureHandler(session_factory)

    decision = await handler.handle(
        ready=ready,
        exc=ValueError("surprise"),
        last_attempt=True,
        analyzable_article_id=article.id,
    )

    assert decision.reraise is False
    assert decision.stage_hold_reason is None


@pytest.mark.asyncio
async def test_audit_failure_falls_back_to_log_with_secrets_redacted(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """audit Repository が raise しても handler は完走し
    ``embedding_failure_audit_dropped`` log にフォールバックする。
    business / audit exception message に混入した secret prefix が log field
    から redact されることも検証する。"""
    article = await _make_article(db_session, sample_source)
    ready = _ready_for()
    handler = EmbeddingFailureHandler(session_factory)

    # business 側の例外は __str__ が code 固定値のみ。
    business_exc = to_embedding_error(AIProviderConfigurationError())

    with (
        patch(
            "app.analysis.embedding.failure_handling.EmbeddingAuditRepository"
        ) as mock_audit_cls,
        capture_logs() as cap,
    ):
        mock_audit_cls.return_value.append_failure = AsyncMock(
            side_effect=RuntimeError(
                "audit db down Authorization: Bearer sk-live-AUDITSECRETxyz"
            )
        )
        # handler は落ちずに完走 (Terminal → reraise=False)
        decision = await handler.handle(
            ready=ready,
            exc=business_exc,
            last_attempt=False,
            analyzable_article_id=article.id,
        )

    assert decision.reraise is False
    assert decision.stage_hold_reason == "ai_error_configuration"
    drops = [e for e in cap if e.get("event") == "embedding_failure_audit_dropped"]
    assert drops, "fallback ログが emit されていない"
    drop = drops[-1]
    assert drop["analyzed_article_id"] == ready.analyzed_article_id
    assert drop["business_error_class"].endswith(".EmbeddingTerminalError")
    assert drop["audit_error_class"].endswith(".RuntimeError")
    # business 側は code 固定値のみなので secret は入らない。
    assert "sk-live" not in drop["business_error_message"]
    # audit 側 (任意 RuntimeError) は redact_secrets で secret が落ちる。
    assert "sk-live-AUDITSECRETxyz" not in drop["audit_error_message"]


# ---------------------------------------------------------------------------
# processing_outcome metric: handler が述語の結果を正しい境界で emit する
# ---------------------------------------------------------------------------
#
# provider marker -> bucket の全 truth table は ``test_ai_provider_outcome.py``
# が正本。ここでは handler が各 arm で述語の結果を正しく emit に転送し、3 値が排他
# であることを代表 marker で固定する。assessment との差 (Recoverable/Terminal arm が
# 一律 failed ではなく provider_error の infra/failed を割る) は Network -> infra_error
# と InputRejected -> failed の対で落ちる。


def _mock_session_factory() -> MagicMock:
    """`async with factory() as s: await s.commit()` を DB 無しで満たす factory。

    metric emit は audit / DB に到達する前に確定するため、これらのテストは実 DB を要さず
    純 unit で metric 契約を固定する。
    """
    session = MagicMock()
    session.commit = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


def _build_outcome_cases() -> list[tuple[BaseException, str]]:
    """各 (handler に渡す exc, 期待 processing_outcome result)。"""
    return [
        # Recoverable + infra: assessment なら failed だが embedding は infra_error。
        (to_embedding_error(AIProviderNetworkError()), "infra_error"),
        # Terminal + infra (Configuration は OPERATOR_ACTION_REQUIRED)。
        (to_embedding_error(AIProviderConfigurationError()), "infra_error"),
        # Terminal + content reject (provider_error は ContentError)。
        (to_embedding_error(_input_rejected()), "failed"),
        # Recoverable + provider_error=None (stage 工程由来)。
        (EmbeddingResponseInvalidError(), "failed"),
        # SQLAlchemyError arm。
        (SQLAlchemyError("db down"), "infra_error"),
        # catch-all (marker いずれにも該当しない)。
        (ValueError("surprise"), "failed"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("exc,expected", _build_outcome_cases())
async def test_handler_emits_classified_processing_outcome(
    capfire: CaptureLogfire,
    exc: BaseException,
    expected: str,
) -> None:
    """handler は各 arm で分類どおりの result を 1 件だけ emit する (3 値排他)。

    audit 副作用をハンドラ上で no-op に差し替え、metric が audit / DB に依らないことを
    純 unit で固定する (emit は audit より先)。
    """
    ready = _ready_for()
    handler = EmbeddingFailureHandler(MagicMock())

    with (
        patch.object(handler, "_audit_failure", new=AsyncMock()),
        patch.object(handler, "_audit_unexpected_failure", new=AsyncMock()),
    ):
        await handler.handle(
            ready=ready, exc=exc, last_attempt=True, analyzable_article_id=1
        )

    metrics = collected_metrics(capfire)
    assert sum_counter_for_result(metrics, _METRIC, expected) == 1
    for other in (r for r in _ALL_RESULTS if r != expected):
        assert sum_counter_for_result(metrics, _METRIC, other) == 0


@pytest.mark.asyncio
async def test_processing_outcome_emitted_even_when_audit_drops(
    capfire: CaptureLogfire,
) -> None:
    """audit drop しても infra_error は emit される (副作用より先に emit するため)。

    実 audit repository を raise させ、``_audit_failure`` の swallow 経路を DB 無しで
    通す。metric は audit より先に確定するため emit は残る。
    """
    ready = _ready_for()
    handler = EmbeddingFailureHandler(_mock_session_factory())
    exc = to_embedding_error(AIProviderNetworkError())

    with patch(
        "app.analysis.embedding.failure_handling.EmbeddingAuditRepository"
    ) as mock_audit_cls:
        mock_audit_cls.STAGE = EmbeddingAuditRepository.STAGE
        mock_audit_cls.return_value.append_failure = AsyncMock(
            side_effect=RuntimeError("audit db down")
        )
        await handler.handle(
            ready=ready, exc=exc, last_attempt=True, analyzable_article_id=1
        )

    metrics = collected_metrics(capfire)
    assert sum_counter_for_result(metrics, _METRIC, "infra_error") == 1
    for other in ("succeeded", "failed"):
        assert sum_counter_for_result(metrics, _METRIC, other) == 0
