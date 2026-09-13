"""Consumer自身の判定・委譲・期限・例外伝播を検証する。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from structlog.testing import capture_logs

from app.analysis.assessment.ai.base import BaseAssessor
from app.analysis.assessment.ai.parse import AssessmentResponseDefect
from app.analysis.assessment.consumer import AssessmentConsumer
from app.analysis.assessment.consumer_failure_classification import (
    classify_assessment_failure,
)
from app.analysis.assessment.domain.ready import (
    AssessmentReadyBuildRejected,
    AssessmentReadyBuildRejectionReason,
    ReadyForAssessment,
)
from app.analysis.assessment.errors import AssessmentResponseInvalidError
from app.analysis.assessment.repository import AssessmentRepository
from app.analysis.assessment.service import (
    AssessmentCompletion,
    AssessmentCompletionKind,
)
from app.analysis.curation.events import ArticleCuratedSignal
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.out_of_scope_article_record import OutOfScopeArticleRecord

_MODULE = "app.analysis.assessment.consumer"


@pytest.fixture
async def target(db_session, sample_source, sample_categories):
    article = AnalyzableArticleRecord(
        source_id=sample_source.id,
        source_url="https://example.com/assessment-consumer",
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
    await db_session.commit()
    return ArticleCuratedSignal(
        curation_id=curation.id, analyzable_article_id=article.id
    )


@pytest.fixture
def consumer(session_factory):
    assessor = MagicMock(spec=BaseAssessor)
    assessor.provider = "deepseek"
    consumer = AssessmentConsumer(session_factory, assessor)
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
        AssessmentCompletion(AssessmentCompletionKind.IN_SCOPE, 123),
        AssessmentCompletion(AssessmentCompletionKind.OUT_OF_SCOPE),
        AssessmentCompletion(AssessmentCompletionKind.ALREADY_ASSESSED),
    ],
)
async def test_ready_is_delegated_and_service_completion_returned(
    consumer, target, completion
):
    """Ready成立時はServiceへ入力を渡し、その完了値をそのまま返す。"""
    consumer._service.execute.return_value = completion

    event = target.model_copy(update={"analyzable_article_id": 999_999})
    result = await consumer.consume(event)

    consumer._service.execute.assert_awaited_once_with(
        ReadyForAssessment(
            curation_id=target.curation_id,
            translated_title="title",
            summary="summary",
        ),
        consumer._assessor,
        analyzable_article_id=target.analyzable_article_id,
    )
    assert result is completion
    consumer._failure_handler.handle.assert_not_awaited()
    consumer._failure_handler.handle_ready_build_rejected.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [AnalyzedArticleRecord, OutOfScopeArticleRecord])
async def test_already_assessed_skips_execution_and_postprocessing(
    db_session, consumer, target, sample_categories, model
):
    """判定済みならServiceも後処理も呼ばず処理済みを返す。"""
    fields = dict(
        curation_id=target.curation_id,
        translated_title="title",
        summary="summary",
        investor_take="take",
    )
    if model is AnalyzedArticleRecord:
        fields["category_id"] = sample_categories[0].id
    db_session.add(model(**fields))
    await db_session.commit()

    result = await consumer.consume(target)

    assert result == AssessmentCompletion(AssessmentCompletionKind.ALREADY_ASSESSED)
    consumer._service.execute.assert_not_awaited()
    consumer._failure_handler.handle.assert_not_awaited()
    consumer._failure_handler.handle_ready_build_rejected.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        AssessmentReadyBuildRejectionReason.CURATION_MISSING,
        AssessmentReadyBuildRejectionReason.INPUT_INVALID,
    ],
)
async def test_rejection_is_passed_unchanged_to_postprocessing(
    consumer, target, reason
):
    """構築拒否はServiceを呼ばず、同じ拒否値を監査処理と呼び出し元へ渡す。"""
    rejected = AssessmentReadyBuildRejected(
        reason,
        None
        if reason is AssessmentReadyBuildRejectionReason.CURATION_MISSING
        else target.analyzable_article_id,
    )
    with patch.object(ReadyForAssessment, "from_facts", return_value=rejected):
        result = await consumer.consume(target)

    assert result is rejected
    handler = consumer._failure_handler.handle_ready_build_rejected
    handler.assert_awaited_once_with(curation_id=target.curation_id, rejected=rejected)
    assert handler.await_args.kwargs["rejected"] is rejected
    consumer._service.execute.assert_not_awaited()
    consumer._failure_handler.handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_facts_are_loaded_once(consumer, target):
    """イベントで指定された記事のDB事実を一度だけ取得してReady判定へ渡す。"""
    load_facts = AssessmentRepository.load_ready_build_facts
    facts_read = []

    async def observe_read(repo, article_id):
        facts = await load_facts(repo, article_id)
        facts_read.append((article_id, facts))
        return facts

    with (
        patch.object(AssessmentRepository, "load_ready_build_facts", new=observe_read),
        patch.object(
            ReadyForAssessment, "from_facts", wraps=ReadyForAssessment.from_facts
        ) as build,
    ):
        await consumer.consume(target)

    assert len(facts_read) == 1
    assert facts_read[0][0] == target.curation_id
    build.assert_called_once_with(target.curation_id, facts_read[0][1])
    assert build.call_args.args[1] is facts_read[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original",
    [
        AssessmentResponseInvalidError(AssessmentResponseDefect.CATEGORY_KEY_MISSING),
        RuntimeError("private-business-error"),
    ],
)
async def test_execution_failure_is_classified_and_reraised(consumer, target, original):
    """実行例外とDB由来記事IDを後処理へ渡し、同じ例外と原因を再送出する。"""
    cause = ValueError("private-cause")
    original.__cause__ = cause
    consumer._service.execute.side_effect = original

    with pytest.raises(type(original)) as raised:
        await consumer.consume(
            target.model_copy(update={"analyzable_article_id": 999_999})
        )

    assert raised.value is original
    assert raised.value.__cause__ is cause
    consumer._failure_handler.handle.assert_awaited_once_with(
        failure=classify_assessment_failure(original),
        exc=original,
        curation_id=target.curation_id,
        analyzable_article_id=target.analyzable_article_id,
        provider="deepseek",
    )
    assert consumer._failure_handler.handle.await_args.kwargs["exc"] is original
    consumer._failure_handler.handle_ready_build_rejected.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_read_failure_does_not_substitute_event_id(
    db_session, test_database_url, consumer, target
):
    """DB事実の取得失敗では、探索IDを監査の記事IDへ補完しない。"""
    await db_session.execute(
        text("LOCK TABLE article_curations IN ACCESS EXCLUSIVE MODE")
    )
    engine = create_async_engine(
        test_database_url, connect_args={"server_settings": {"lock_timeout": "100ms"}}
    )
    try:
        consumer._session_factory = async_sessionmaker(engine, expire_on_commit=False)
        with pytest.raises(DBAPIError) as raised:
            await consumer.consume(target)
        consumer._failure_handler.handle.assert_awaited_once_with(
            failure=classify_assessment_failure(raised.value),
            exc=raised.value,
            curation_id=target.curation_id,
            analyzable_article_id=None,
            provider="deepseek",
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
    load_facts = AssessmentRepository.load_ready_build_facts

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
        patch.object(AssessmentRepository, "load_ready_build_facts", new=observe_read),
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
            ArticleCuratedSignal(curation_id=999_999, analyzable_article_id=999_999)
        )

    assert result == AssessmentReadyBuildRejected(
        AssessmentReadyBuildRejectionReason.CURATION_MISSING
    )
    consumer._failure_handler.handle_ready_build_rejected.assert_awaited_once()
    consumer._failure_handler.handle.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["classification", "handler", "handler_and_logger"]
)
async def test_secondary_failure_preserves_original(consumer, target, operation):
    """分類・後処理・診断の障害で元の実行例外を置き換えない。"""
    original = AssessmentResponseInvalidError(
        AssessmentResponseDefect.CATEGORY_KEY_MISSING
    )
    consumer._service.execute.side_effect = original
    boundary = (
        patch(
            f"{_MODULE}.classify_assessment_failure",
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
        event = ArticleCuratedSignal(curation_id=999_999, analyzable_article_id=999_999)
        consumer._failure_handler.handle_ready_build_rejected.side_effect = cancelled
    else:
        event = target
        consumer._service.execute.side_effect = AssessmentResponseInvalidError(
            AssessmentResponseDefect.CATEGORY_KEY_MISSING
        )
        consumer._failure_handler.handle.side_effect = cancelled

    with pytest.raises(asyncio.CancelledError) as raised:
        await consumer.consume(event)

    assert raised.value is cancelled
