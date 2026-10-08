"""Consumer自身の判定・委譲・期限・再試行の判断を検証する。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ai_providers.errors import (
    AIProviderResponseError,
    AIProviderResponseReason,
)
from app.analysis.assessment.events import ArticleAssessedInScope
from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.consumer import EmbeddingConsumer
from app.analysis.embedding.consumer_failure_classification import (
    NoRetryEmbedding,
    RetryEmbedding,
)
from app.analysis.embedding.domain.ready import (
    EmbeddingReadyBuildRejected,
    EmbeddingReadyBuildRejectionReason,
    ReadyForEmbedding,
)
from app.analysis.embedding.domain.value_objects import EMBEDDING_DIMENSION
from app.analysis.embedding.errors import (
    EmbeddingAnalyzedArticleMissingError,
    EmbeddingResponseInvalidError,
)
from app.analysis.embedding.repository import EmbeddingRepository
from app.analysis.embedding.service import EmbeddingCompletion
from app.analysis.logging import create_article_analysis_logger
from app.http.errors import HttpResponseError
from app.models.analyzed_article_record import AnalyzedArticleRecord

_MODULE = "app.analysis.embedding.consumer"
_RECEIVED_AT = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def embedding_logger():
    return create_article_analysis_logger().bind(stage="embedding")


@pytest.fixture
def target(embedding_target):
    return embedding_target[0]


@pytest.fixture
def article_id(embedding_target):
    return embedding_target[1]


@pytest.fixture
def consumer(session_factory):
    embedder = MagicMock(spec=BaseEmbedder)
    embedder.provider = "gemini"
    consumer = EmbeddingConsumer(session_factory, embedder)
    with (
        patch.object(consumer._service, "execute", new_callable=AsyncMock),
        patch.object(consumer._failure_handler, "handle", new_callable=AsyncMock),
        patch.object(
            consumer._failure_handler,
            "handle_ready_build_rejected",
            new_callable=AsyncMock,
        ),
    ):
        yield consumer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "completion",
    [
        EmbeddingCompletion.SAVED,
        EmbeddingCompletion.ALREADY_EMBEDDED,
    ],
)
async def test_ready_is_delegated_and_service_completion_returned(
    consumer, target, completion, article_id, embedding_logger
):
    """Ready成立時はServiceへ入力を渡し、その完了値をそのまま返す。"""
    consumer._service.execute.return_value = completion

    event = target.model_copy(update={"curation_id": 999_999})
    result = await consumer.consume(event, logger=embedding_logger)

    consumer._service.execute.assert_awaited_once_with(
        ReadyForEmbedding(
            analyzed_article_id=target.analyzed_article_id,
            text_for_embedding="summary",
        ),
        consumer._embedder,
        analyzable_article_id=article_id,
        logger=embedding_logger,
    )
    assert result is completion
    consumer._failure_handler.handle.assert_not_awaited()
    consumer._failure_handler.handle_ready_build_rejected.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_embedded_skips_execution_and_postprocessing(
    db_session, consumer, target, embedding_logger
):
    """生成済みならServiceも後処理も呼ばず処理済みを返す。"""
    stored = await db_session.get(AnalyzedArticleRecord, target.analyzed_article_id)
    stored.embedding = [0.4] * EMBEDDING_DIMENSION
    await db_session.commit()

    result = await consumer.consume(target, logger=embedding_logger)

    assert result is EmbeddingCompletion.ALREADY_EMBEDDED
    consumer._service.execute.assert_not_awaited()
    consumer._failure_handler.handle.assert_not_awaited()
    consumer._failure_handler.handle_ready_build_rejected.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        EmbeddingReadyBuildRejectionReason.ANALYZED_ARTICLE_MISSING,
        EmbeddingReadyBuildRejectionReason.INPUT_INVALID,
    ],
)
async def test_rejection_is_passed_unchanged_to_postprocessing(
    consumer, target, reason, article_id, embedding_logger
):
    """構築拒否はServiceを呼ばず、同じ拒否値を監査処理へ渡し再試行しない。"""
    rejected = EmbeddingReadyBuildRejected(
        reason,
        None
        if reason is EmbeddingReadyBuildRejectionReason.ANALYZED_ARTICLE_MISSING
        else article_id,
    )
    with patch.object(ReadyForEmbedding, "from_facts", return_value=rejected):
        result = await consumer.consume(target, logger=embedding_logger)

    assert result == NoRetryEmbedding(rejected)
    assert result.cause is rejected
    handler = consumer._failure_handler.handle_ready_build_rejected
    handler.assert_awaited_once_with(
        analyzed_article_id=target.analyzed_article_id,
        rejected=rejected,
        logger=embedding_logger,
    )
    assert handler.await_args.kwargs["rejected"] is rejected
    consumer._service.execute.assert_not_awaited()
    consumer._failure_handler.handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_facts_are_loaded_once(consumer, target, embedding_logger):
    """イベントで指定された記事のDB事実を一度だけ取得してReady判定へ渡す。"""
    load_facts = EmbeddingRepository.load_ready_build_facts
    facts_read = []

    async def observe_read(repo, article_id):
        facts = await load_facts(repo, article_id)
        facts_read.append((article_id, facts))
        return facts

    with (
        patch.object(EmbeddingRepository, "load_ready_build_facts", new=observe_read),
        patch.object(
            ReadyForEmbedding, "from_facts", wraps=ReadyForEmbedding.from_facts
        ) as build,
    ):
        await consumer.consume(target, logger=embedding_logger)

    assert len(facts_read) == 1
    assert facts_read[0][0] == target.analyzed_article_id
    build.assert_called_once_with(target.analyzed_article_id, facts_read[0][1])
    assert build.call_args.args[1] is facts_read[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original",
    [
        EmbeddingResponseInvalidError(),
        RuntimeError("private-business-error"),
        AIProviderResponseError(
            reason=AIProviderResponseReason.RATE_LIMITED,
            http_error=HttpResponseError(status_code=429, received_at=_RECEIVED_AT),
        ),
    ],
)
async def test_execution_failure_is_handled_and_left_to_retry(
    consumer, target, original, article_id, embedding_logger
):
    """実行例外とDB由来記事IDを後処理へ渡し、同じ例外と原因を持ったまま再試行に回す。"""
    cause = ValueError("private-cause")
    original.__cause__ = cause
    consumer._service.execute.side_effect = original

    result = await consumer.consume(
        target.model_copy(update={"curation_id": 999_999}), logger=embedding_logger
    )

    assert result == RetryEmbedding(original)
    assert result.error is original
    assert original.__cause__ is cause
    consumer._failure_handler.handle.assert_awaited_once_with(
        failure=RetryEmbedding(original),
        exc=original,
        analyzed_article_id=target.analyzed_article_id,
        analyzable_article_id=article_id,
        provider="gemini",
        logger=embedding_logger,
    )
    assert consumer._failure_handler.handle.await_args.kwargs["exc"] is original
    consumer._failure_handler.handle_ready_build_rejected.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original",
    [
        pytest.param(
            AIProviderResponseError(
                reason=AIProviderResponseReason.INPUT_BLOCKED,
                http_error=HttpResponseError(status_code=400, received_at=_RECEIVED_AT),
            ),
            id="input_blocked",
        ),
        pytest.param(EmbeddingAnalyzedArticleMissingError(), id="article_missing"),
    ],
)
async def test_failure_unrecoverable_by_redelivery_is_not_retried_after_handling(
    consumer, target, original, article_id, embedding_logger
):
    """同じ入力では回復しないAIの失敗と保存時の対象なしは、後処理のあと再試行しない。"""
    consumer._service.execute.side_effect = original

    result = await consumer.consume(target, logger=embedding_logger)

    assert result == NoRetryEmbedding(original)
    consumer._failure_handler.handle.assert_awaited_once_with(
        failure=NoRetryEmbedding(original),
        exc=original,
        analyzed_article_id=target.analyzed_article_id,
        analyzable_article_id=article_id,
        provider="gemini",
        logger=embedding_logger,
    )
    consumer._failure_handler.handle_ready_build_rejected.assert_not_awaited()


@pytest.mark.asyncio
async def test_decision_survives_failure_handling_error(
    consumer, target, embedding_logger
):
    """後処理が失敗しても、決めた扱いを変えない。"""
    provider_error = AIProviderResponseError(
        reason=AIProviderResponseReason.INPUT_TOO_LONG,
        http_error=HttpResponseError(status_code=400, received_at=_RECEIVED_AT),
    )
    consumer._service.execute.side_effect = provider_error
    consumer._failure_handler.handle.side_effect = RuntimeError("secondary-secret")

    result = await consumer.consume(target, logger=embedding_logger)

    assert result == NoRetryEmbedding(provider_error)


@pytest.mark.asyncio
async def test_classification_failure_propagates_for_redelivery(
    consumer, target, embedding_logger
):
    """扱いを決められなければ、その例外を伝えて再配信に任せ、元の例外を原因に残す。"""
    original = AIProviderResponseError(
        reason=AIProviderResponseReason.INPUT_BLOCKED,
        http_error=HttpResponseError(status_code=400, received_at=_RECEIVED_AT),
    )
    consumer._service.execute.side_effect = original
    classification_error = RuntimeError("classification-bug")

    with (
        patch(
            f"{_MODULE}.classify_embedding_failure",
            side_effect=classification_error,
        ),
        pytest.raises(RuntimeError) as raised,
    ):
        await consumer.consume(target, logger=embedding_logger)

    assert raised.value is classification_error
    assert raised.value.__context__ is original
    consumer._failure_handler.handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_read_failure_does_not_substitute_event_id(
    db_session, test_database_url, consumer, target, embedding_logger
):
    """DB事実の取得失敗では、探索IDを監査の記事IDへ補完しない。"""
    await db_session.execute(
        text("LOCK TABLE analyzed_articles IN ACCESS EXCLUSIVE MODE")
    )
    engine = create_async_engine(
        test_database_url, connect_args={"server_settings": {"lock_timeout": "100ms"}}
    )
    try:
        consumer._session_factory = async_sessionmaker(engine, expire_on_commit=False)
        result = await consumer.consume(target, logger=embedding_logger)
        assert isinstance(result, RetryEmbedding)
        assert isinstance(result.error, DBAPIError)
        consumer._failure_handler.handle.assert_awaited_once_with(
            failure=result,
            exc=result.error,
            analyzed_article_id=target.analyzed_article_id,
            analyzable_article_id=None,
            provider="gemini",
            logger=embedding_logger,
        )
        consumer._service.execute.assert_not_awaited()
    finally:
        await db_session.rollback()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["read", "execute"])
async def test_business_timeout_ends_before_failure_handling(
    consumer, target, phase, embedding_logger
):
    """取得・実行中の期限切れは再試行に回し、後処理はタイマー解除後に呼ぶ。"""
    business_timeout = asyncio.timeout(None)
    load_facts = EmbeddingRepository.load_ready_build_facts

    async def expire(*args, **kwargs):
        business_timeout.reschedule(asyncio.get_running_loop().time())
        await asyncio.Event().wait()

    async def observe_read(repo, article_id):
        facts = await load_facts(repo, article_id)
        if phase == "read":
            await expire()
        return facts

    async def handle_after_deadline(**kwargs):
        assert business_timeout.expired()
        with pytest.raises(RuntimeError, match="finished"):
            business_timeout.reschedule(asyncio.get_running_loop().time())

    consumer._service.execute.side_effect = expire
    consumer._failure_handler.handle.side_effect = handle_after_deadline
    with (
        patch(f"{_MODULE}.timeout", return_value=business_timeout),
        patch.object(EmbeddingRepository, "load_ready_build_facts", new=observe_read),
    ):
        result = await consumer.consume(target, logger=embedding_logger)

    assert isinstance(result, RetryEmbedding)
    assert isinstance(result.error, TimeoutError)
    consumer._failure_handler.handle.assert_awaited_once()
    assert consumer._failure_handler.handle.await_args.kwargs["exc"] is result.error
    if phase == "read":
        consumer._service.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejection_handling_runs_after_business_timeout(
    consumer, embedding_logger
):
    """拒否確定後の監査は業務タイマーを解除してから実行する。"""
    business_timeout = asyncio.timeout(None)

    async def handle_after_deadline(**kwargs):
        with pytest.raises(RuntimeError, match="finished"):
            business_timeout.reschedule(asyncio.get_running_loop().time())

    consumer._failure_handler.handle_ready_build_rejected.side_effect = (
        handle_after_deadline
    )
    with patch(f"{_MODULE}.timeout", return_value=business_timeout):
        result = await consumer.consume(
            ArticleAssessedInScope(curation_id=999_999, analyzed_article_id=999_999),
            logger=embedding_logger,
        )

    assert result == NoRetryEmbedding(
        EmbeddingReadyBuildRejected(
            EmbeddingReadyBuildRejectionReason.ANALYZED_ARTICLE_MISSING
        )
    )
    consumer._failure_handler.handle_ready_build_rejected.assert_awaited_once()
    consumer._failure_handler.handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_secondary_failure_preserves_original(consumer, target, embedding_logger):
    """後処理の障害で、元の実行例外と決めた扱いを置き換えない。"""
    original = EmbeddingResponseInvalidError()
    consumer._service.execute.side_effect = original
    with patch.object(
        consumer._failure_handler,
        "handle",
        side_effect=RuntimeError("secondary-secret"),
    ):
        result = await consumer.consume(target, logger=embedding_logger)

    assert result == RetryEmbedding(original)


@pytest.mark.asyncio
async def test_cancellation_bypasses_failure_handling(
    consumer, target, embedding_logger
):
    """実行中の外部キャンセルは失敗後処理を呼ばず伝播する。"""
    started = asyncio.Event()

    async def wait_cancel(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    consumer._service.execute.side_effect = wait_cancel
    task = asyncio.create_task(consumer.consume(target, logger=embedding_logger))
    try:
        async with asyncio.timeout(5):
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    consumer._failure_handler.handle.assert_not_awaited()
    consumer._failure_handler.handle_ready_build_rejected.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["ready_rejection", "execution_failure"])
async def test_postprocessing_cancellation_propagates(
    consumer, target, phase, embedding_logger
):
    """後処理からのキャンセルを通常の二次障害として抑止しない。"""
    cancelled = asyncio.CancelledError()
    if phase == "ready_rejection":
        event = ArticleAssessedInScope(curation_id=999_999, analyzed_article_id=999_999)
        consumer._failure_handler.handle_ready_build_rejected.side_effect = cancelled
    else:
        event = target
        consumer._service.execute.side_effect = EmbeddingResponseInvalidError()
        consumer._failure_handler.handle.side_effect = cancelled

    with pytest.raises(asyncio.CancelledError) as raised:
        await consumer.consume(event, logger=embedding_logger)

    assert raised.value is cancelled
