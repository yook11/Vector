"""Consumer自身の判定・委譲・期限・例外伝播を検証する。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from structlog.testing import capture_logs

from app.analysis.assessment.events import ArticleAssessedInScope
from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.consumer import EmbeddingConsumer
from app.analysis.embedding.consumer_failure_classification import (
    classify_embedding_failure,
)
from app.analysis.embedding.domain.ready import (
    EmbeddingReadyBuildRejected,
    EmbeddingReadyBuildRejectionReason,
    ReadyForEmbedding,
)
from app.analysis.embedding.domain.value_objects import EMBEDDING_DIMENSION
from app.analysis.embedding.errors import EmbeddingResponseInvalidError
from app.analysis.embedding.repository import EmbeddingRepository
from app.analysis.embedding.service import EmbeddingCompletion
from app.models.analyzed_article_record import AnalyzedArticleRecord

_MODULE = "app.analysis.embedding.consumer"


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
    consumer, target, completion, article_id
):
    """Ready成立時はServiceへ入力を渡し、その完了値をそのまま返す。"""
    consumer._service.execute.return_value = completion

    event = target.model_copy(update={"curation_id": 999_999})
    result = await consumer.consume(event)

    consumer._service.execute.assert_awaited_once_with(
        ReadyForEmbedding(
            analyzed_article_id=target.analyzed_article_id,
            text_for_embedding="summary",
        ),
        consumer._embedder,
        analyzable_article_id=article_id,
    )
    assert result is completion
    consumer._failure_handler.handle.assert_not_awaited()
    consumer._failure_handler.handle_ready_build_rejected.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_embedded_skips_execution_and_postprocessing(
    db_session, consumer, target
):
    """生成済みならServiceも後処理も呼ばず処理済みを返す。"""
    stored = await db_session.get(AnalyzedArticleRecord, target.analyzed_article_id)
    stored.embedding = [0.4] * EMBEDDING_DIMENSION
    await db_session.commit()

    result = await consumer.consume(target)

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
    consumer, target, reason, article_id
):
    """構築拒否はServiceを呼ばず、同じ拒否値を監査処理と呼び出し元へ渡す。"""
    rejected = EmbeddingReadyBuildRejected(
        reason,
        None
        if reason is EmbeddingReadyBuildRejectionReason.ANALYZED_ARTICLE_MISSING
        else article_id,
    )
    with patch.object(ReadyForEmbedding, "from_facts", return_value=rejected):
        result = await consumer.consume(target)

    assert result is rejected
    handler = consumer._failure_handler.handle_ready_build_rejected
    handler.assert_awaited_once_with(
        analyzed_article_id=target.analyzed_article_id, rejected=rejected
    )
    assert handler.await_args.kwargs["rejected"] is rejected
    consumer._service.execute.assert_not_awaited()
    consumer._failure_handler.handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_facts_are_loaded_once(consumer, target):
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
        await consumer.consume(target)

    assert len(facts_read) == 1
    assert facts_read[0][0] == target.analyzed_article_id
    build.assert_called_once_with(target.analyzed_article_id, facts_read[0][1])
    assert build.call_args.args[1] is facts_read[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original",
    [EmbeddingResponseInvalidError(), RuntimeError("private-business-error")],
)
async def test_execution_failure_is_classified_and_reraised(
    consumer, target, original, article_id
):
    """実行例外とDB由来記事IDを後処理へ渡し、同じ例外と原因を再送出する。"""
    cause = ValueError("private-cause")
    original.__cause__ = cause
    consumer._service.execute.side_effect = original

    with pytest.raises(type(original)) as raised:
        await consumer.consume(target.model_copy(update={"curation_id": 999_999}))

    assert raised.value is original
    assert raised.value.__cause__ is cause
    consumer._failure_handler.handle.assert_awaited_once_with(
        failure=classify_embedding_failure(original),
        exc=original,
        analyzed_article_id=target.analyzed_article_id,
        analyzable_article_id=article_id,
        provider="gemini",
    )
    assert consumer._failure_handler.handle.await_args.kwargs["exc"] is original
    consumer._failure_handler.handle_ready_build_rejected.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_read_failure_does_not_substitute_event_id(
    db_session, test_database_url, consumer, target
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
        with pytest.raises(DBAPIError) as raised:
            await consumer.consume(target)
        consumer._failure_handler.handle.assert_awaited_once_with(
            failure=classify_embedding_failure(raised.value),
            exc=raised.value,
            analyzed_article_id=target.analyzed_article_id,
            analyzable_article_id=None,
            provider="gemini",
        )
        consumer._service.execute.assert_not_awaited()
    finally:
        await db_session.rollback()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["read", "execute"])
async def test_business_timeout_ends_before_failure_handling(consumer, target, phase):
    """取得・実行中の期限切れを伝播し、後処理はタイマー解除後に呼ぶ。"""
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
        pytest.raises(TimeoutError) as raised,
    ):
        await consumer.consume(target)

    consumer._failure_handler.handle.assert_awaited_once()
    assert consumer._failure_handler.handle.await_args.kwargs["exc"] is raised.value
    if phase == "read":
        consumer._service.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejection_handling_runs_after_business_timeout(consumer):
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
            ArticleAssessedInScope(curation_id=999_999, analyzed_article_id=999_999)
        )

    assert result == EmbeddingReadyBuildRejected(
        EmbeddingReadyBuildRejectionReason.ANALYZED_ARTICLE_MISSING
    )
    consumer._failure_handler.handle_ready_build_rejected.assert_awaited_once()
    consumer._failure_handler.handle.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["classification", "handler", "handler_and_logger"]
)
async def test_secondary_failure_preserves_original(consumer, target, operation):
    """分類・後処理・診断の障害で元の実行例外を置き換えない。"""
    original = EmbeddingResponseInvalidError()
    consumer._service.execute.side_effect = original
    boundary = (
        patch(
            f"{_MODULE}.classify_embedding_failure",
            side_effect=RuntimeError("secondary-secret"),
        )
        if operation == "classification"
        else patch.object(
            consumer._failure_handler,
            "handle",
            side_effect=RuntimeError("secondary-secret"),
        )
    )
    with capture_logs() as logs, boundary:
        if operation == "handler_and_logger":
            with (
                patch(
                    f"{_MODULE}.logger.warning", side_effect=RuntimeError("log-secret")
                ),
                pytest.raises(type(original)) as raised,
            ):
                await consumer.consume(target)
        else:
            with pytest.raises(type(original)) as raised:
                await consumer.consume(target)

    assert raised.value is original
    assert "secondary-secret" not in str(logs)


@pytest.mark.asyncio
async def test_cancellation_bypasses_failure_handling(consumer, target):
    """実行中の外部キャンセルは失敗後処理を呼ばず伝播する。"""
    started = asyncio.Event()

    async def wait_cancel(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    consumer._service.execute.side_effect = wait_cancel
    task = asyncio.create_task(consumer.consume(target))
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
async def test_postprocessing_cancellation_propagates(consumer, target, phase):
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
        await consumer.consume(event)

    assert raised.value is cancelled
