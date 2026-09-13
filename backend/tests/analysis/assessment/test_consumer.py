"""Consumerの正常終了・DB境界・失敗伝播を実DBで検証する。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.ai_providers.errors import (
    AIProviderConfigurationError,
    AIProviderNetworkError,
    AIProviderUsageLimitExhaustedError,
)
from app.analysis.assessment.ai.base import BaseAssessor
from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.ai.parse import AssessmentResponseDefect
from app.analysis.assessment.consumer import AssessmentConsumer
from app.analysis.assessment.domain.ready import (
    AssessmentReadyBuildRejected,
    AssessmentReadyBuildRejectionReason,
    ReadyForAssessment,
)
from app.analysis.assessment.domain.result import InScope, InScopeCategory, OutOfScope
from app.analysis.assessment.errors import (
    AssessmentError,
    AssessmentResponseInvalidError,
)
from app.analysis.assessment.events import ArticleAssessedInScope
from app.analysis.assessment.service import (
    AssessmentCompletion,
    AssessmentCompletionKind,
)
from app.analysis.curation.events import ArticleCuratedSignal
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.out_of_scope_article_record import OutOfScopeArticleRecord
from app.models.outbox_event import OutboxEvent
from app.models.pipeline_event import PipelineEvent
from tests.cloudwatch.records import metric_records
from tests.outbox import RejectOutboxInsert

_MODULE = "app.analysis.assessment.consumer"


def _call(in_scope=True):
    return AssessmentCall(
        result=InScope(category=InScopeCategory.AI, investor_take="bullish")
        if in_scope
        else OutOfScope(investor_take="not relevant"),
        raw_response='{"category":"ai"}' if in_scope else '{"category":"out_of_scope"}',
        raw_category="ai" if in_scope else "out_of_scope",
        prompt_version="testver1",
        model_name="test-model",
    )


@pytest.fixture
def assessor():
    fake = MagicMock(spec=BaseAssessor)
    fake.provider = "deepseek"
    fake.assess = AsyncMock(return_value=_call())
    return fake


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


async def _events(session):
    return list(
        (await session.scalars(select(PipelineEvent).order_by(PipelineEvent.id))).all()
    )


async def _rows(session, model):
    return list((await session.scalars(select(model))).all())


@pytest.mark.asyncio
@pytest.mark.parametrize("in_scope", [True, False])
async def test_uses_db_identity_and_skips_ai_and_metrics_when_assessed(
    db_session, session_factory, target, assessor, capsys, in_scope
):
    """監査はDB由来IDを使い、開始時の判定済みではAIと成功メトリクスを追加しない。"""
    assessor.assess.return_value = _call(in_scope)
    event = target.model_copy(update={"analyzable_article_id": 999_999})
    consumer = AssessmentConsumer(session_factory, assessor)
    result = await consumer.consume(event)
    rows = await _rows(
        db_session, AnalyzedArticleRecord if in_scope else OutOfScopeArticleRecord
    )
    assert len(rows) == 1
    assert rows[0].curation_id == target.curation_id
    assert result == (
        AssessmentCompletion(AssessmentCompletionKind.IN_SCOPE, rows[0].id)
        if in_scope
        else AssessmentCompletion(AssessmentCompletionKind.OUT_OF_SCOPE)
    )
    events = await _events(db_session)
    assert len(events) == 1 and events[0].event_type == "succeeded"
    assert events[0].article_id == target.analyzable_article_id
    assert [
        r["result"]
        for r in metric_records(capsys.readouterr().out, "processing_outcome")
    ] == ["in_scope" if in_scope else "out_of_scope"]
    again = await consumer.consume(event)
    assert again == AssessmentCompletion(AssessmentCompletionKind.ALREADY_ASSESSED)
    assessor.assess.assert_awaited_once_with(title_ja="title", summary_ja="summary")
    assert metric_records(capsys.readouterr().out, "processing_outcome") == []


@pytest.mark.asyncio
async def test_missing_curation_keeps_only_event_curation_id(
    db_session, session_factory, target, assessor
):
    """対象欠損をイベントの記事IDで補完せず拒否として記録する。"""
    event = target.model_copy(update={"curation_id": 999_999})
    result = await AssessmentConsumer(session_factory, assessor).consume(event)
    assert result == AssessmentReadyBuildRejected(
        AssessmentReadyBuildRejectionReason.CURATION_MISSING
    )
    assessor.assess.assert_not_awaited()
    events = await _events(db_session)
    assert len(events) == 1
    assert events[0].event_type == "rejected"
    assert events[0].article_id is None
    assert events[0].outcome_code == "assessment_ready_build_blocked_curation_missing"
    assert events[0].payload["curation_id"] == 999_999


@pytest.mark.asyncio
async def test_invalid_ready_preserves_known_db_article_id(
    db_session, session_factory, target, assessor, capsys
):
    """入力不正はDB由来IDで拒否記録し、記事保持のまま受信完了する。"""
    from_facts = ReadyForAssessment.from_facts
    rejected_results = []

    def invalid_from_facts(curation_id, facts):
        rejected = from_facts(curation_id, replace(facts, summary=""))
        rejected_results.append(rejected)
        return rejected

    with patch.object(ReadyForAssessment, "from_facts", side_effect=invalid_from_facts):
        result = await AssessmentConsumer(session_factory, assessor).consume(
            target.model_copy(update={"analyzable_article_id": 999_999})
        )
    assert result is rejected_results[0]
    assert result == AssessmentReadyBuildRejected(
        AssessmentReadyBuildRejectionReason.INPUT_INVALID, target.analyzable_article_id
    )
    assessor.assess.assert_not_awaited()
    events = await _events(db_session)
    assert len(events) == 1 and events[0].article_id == target.analyzable_article_id
    assert events[0].event_type == "rejected"
    assert events[0].outcome_code == "assessment_ready_build_blocked_input_invalid"
    assert events[0].payload.get("input_text") is None
    assert events[0].payload.get("error_chain") is None
    assert (
        await db_session.get(AnalyzableArticleRecord, target.analyzable_article_id)
        is not None
    )
    assert await db_session.get(ArticleCuration, target.curation_id) is not None
    assert await _rows(db_session, AnalyzedArticleRecord) == []
    assert await _rows(db_session, OutOfScopeArticleRecord) == []
    assert await _rows(db_session, OutboxEvent) == []
    assert metric_records(capsys.readouterr().out, "processing_outcome") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original",
    [
        AIProviderNetworkError(),
        AIProviderConfigurationError(),
        AIProviderUsageLimitExhaustedError(),
    ],
)
async def test_provider_failure_is_audited_and_always_propagated(
    db_session, session_factory, target, assessor, original
):
    """監査のretryabilityにかかわらずプロバイダー失敗を伝播する。"""
    assessor.assess.side_effect = original
    with pytest.raises(AssessmentError) as raised:
        await AssessmentConsumer(session_factory, assessor).consume(target)
    assert raised.value.provider_error is original
    assert raised.value.__cause__ is original
    events = await _events(db_session)
    assert len(events) == 1 and events[0].event_type == "failed"
    assert events[0].outcome_code == original.CODE
    assert await _rows(db_session, AnalyzedArticleRecord) == []
    assert await _rows(db_session, OutboxEvent) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original",
    [
        AssessmentResponseInvalidError(AssessmentResponseDefect.CATEGORY_KEY_MISSING),
        RuntimeError("unexpected"),
    ],
)
async def test_stage_failure_keeps_original_instance(
    db_session, session_factory, target, assessor, original
):
    """応答不正や想定外例外を再分類した例外で置き換えない。"""
    assessor.assess.side_effect = original
    with pytest.raises(type(original)) as raised:
        await AssessmentConsumer(session_factory, assessor).consume(target)
    assert raised.value is original
    assert len(await _events(db_session)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["read", "ai", "save"])
async def test_timeout_covers_business_but_not_failure_handling(
    db_session, session_factory, target, assessor, phase
):
    """状態取得・AI・保存の期限切れ後も、期限外で失敗監査を完了できる。"""
    if phase == "read":
        await db_session.execute(
            text("LOCK TABLE article_curations IN ACCESS EXCLUSIVE MODE")
        )
    elif phase == "ai":

        async def wait_forever(**kwargs):
            await asyncio.Event().wait()

        assessor.assess.side_effect = wait_forever
    else:

        async def lock_before_save(**kwargs):
            await db_session.execute(
                text("LOCK TABLE analyzed_articles IN ACCESS EXCLUSIVE MODE")
            )
            return _call()

        assessor.assess.side_effect = lock_before_save
    consumer = AssessmentConsumer(session_factory, assessor)
    handle = consumer._failure_handler.handle

    async def slow_handler(**kwargs):
        await asyncio.sleep(0.15)
        return await handle(**kwargs)

    try:
        with (
            patch(
                f"{_MODULE}.timeout", side_effect=lambda delay: asyncio.timeout(0.1)
            ) as timer,
            patch.object(consumer._failure_handler, "handle", side_effect=slow_handler),
            pytest.raises(TimeoutError),
        ):
            await consumer.consume(target)
        timer.assert_called_once_with(60)
        events = await _events(db_session)
        assert len(events) == 1 and events[0].outcome_code == "unexpected_error"
        assert events[0].article_id == (
            None if phase == "read" else target.analyzable_article_id
        )
        if phase == "read":
            assessor.assess.assert_not_awaited()
    finally:
        await db_session.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["classification", "handler"])
@pytest.mark.parametrize("broken_logger", [False, True])
async def test_secondary_failure_preserves_original(
    session_factory, target, assessor, operation, broken_logger
):
    """分類・後処理・診断が壊れても最初の処理例外を再送出する。"""
    original = AssessmentResponseInvalidError(
        AssessmentResponseDefect.CATEGORY_KEY_MISSING
    )
    assessor.assess.side_effect = original
    consumer = AssessmentConsumer(session_factory, assessor)
    boundary = (
        patch(
            f"{_MODULE}.classify_assessment_failure",
            side_effect=RuntimeError("secondary-secret"),
        )
        if operation == "classification"
        else patch.object(
            consumer._failure_handler,
            "handle",
            new=AsyncMock(side_effect=RuntimeError("secondary-secret")),
        )
    )
    with capture_logs() as logs, boundary:
        if broken_logger:
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
async def test_cancellation_bypasses_failure_handling(
    db_session, session_factory, target, assessor
):
    """外部キャンセルは業務失敗に変換せずそのまま伝播する。"""
    started = asyncio.Event()

    async def wait_cancel(**kwargs):
        started.set()
        await asyncio.Event().wait()

    assessor.assess.side_effect = wait_cancel
    task = asyncio.create_task(
        AssessmentConsumer(session_factory, assessor).consume(target)
    )
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
    assert await _events(db_session) == []


@pytest.mark.asyncio
async def test_loads_ready_facts_once(session_factory, target, assessor):
    """開始条件の事実取得は追加照会をせず1回だけ実行する。"""
    engine = session_factory.kw["bind"].sync_engine
    reads = []

    def before_execute(conn, cursor, statement, parameters, context, executemany):
        if "from article_curations" in statement.lower():
            reads.append(statement)

    sqlalchemy_event.listen(engine, "before_cursor_execute", before_execute)
    try:
        await AssessmentConsumer(session_factory, assessor).consume(target)
        assert len(reads) == 1
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", before_execute)


async def _assert_rolled_back_with_failure(db_session):
    assert await _rows(db_session, AnalyzedArticleRecord) == []
    assert await _rows(db_session, OutOfScopeArticleRecord) == []
    assert await _rows(db_session, OutboxEvent) == []
    events = await _events(db_session)
    assert len(events) == 1 and events[0].event_type == "failed"


@pytest.mark.asyncio
async def test_outbox_failure_rolls_back_and_records_failure(
    db_session,
    session_factory,
    target,
    assessor,
    reject_outbox_insert: RejectOutboxInsert,
):
    """実DBでOutboxを拒否すると保存・成功監査も戻り、失敗監査が残る。"""
    await reject_outbox_insert(ArticleAssessedInScope.EVENT_TYPE)
    with pytest.raises(IntegrityError):
        await AssessmentConsumer(session_factory, assessor).consume(target)
    async with session_factory() as reader:
        await _assert_rolled_back_with_failure(reader)


@pytest.mark.asyncio
@pytest.mark.parametrize("in_scope", [True, False])
async def test_commit_failure_rolls_back_and_uses_separate_failure_transaction(
    db_session, session_factory, target, assessor, in_scope
):
    """保存commitの障害後も別セッションで失敗監査をcommitする。"""
    assessor.assess.return_value = _call(in_scope)
    original = RuntimeError("commit failed")
    commits = []

    class FirstCommitFails(AsyncSession):
        async def commit(self):
            commits.append(self)
            if len(commits) == 1:
                raise original
            await super().commit()

    factory = async_sessionmaker(
        session_factory.kw["bind"], class_=FirstCommitFails, expire_on_commit=False
    )
    with pytest.raises(RuntimeError) as raised:
        await AssessmentConsumer(factory, assessor).consume(target)
    assert raised.value is original
    assert len(commits) == 2 and commits[0] is not commits[1]
    await _assert_rolled_back_with_failure(db_session)


@pytest.mark.asyncio
async def test_success_audit_failure_rolls_back_and_records_failure(
    db_session, session_factory, target, assessor
):
    """成功監査の主キー違反で保存を戻し、失敗監査だけを追加する。"""
    baseline = PipelineEvent(
        id=1,
        stage="assessment",
        event_type="succeeded",
        outcome_code="baseline",
        payload={},
    )
    db_session.add(baseline)
    await db_session.commit()
    with pytest.raises(IntegrityError):
        await AssessmentConsumer(session_factory, assessor).consume(target)
    assert await _rows(db_session, AnalyzedArticleRecord) == []
    assert await _rows(db_session, OutOfScopeArticleRecord) == []
    assert await _rows(db_session, OutboxEvent) == []
    events = await _events(db_session)
    assert len(events) == 2 and events[1].event_type == "failed"


@pytest.mark.asyncio
async def test_deletion_during_ai_preserves_db_failure(
    db_session, session_factory, target, assessor
):
    """AI中の対象削除は既存のDB保存エラーとして伝播する。"""

    async def delete_curation(**kwargs):
        async with session_factory() as session:
            await session.execute(
                delete(ArticleCuration).where(ArticleCuration.id == target.curation_id)
            )
            await session.commit()
        return _call()

    assessor.assess.side_effect = delete_curation
    with pytest.raises(IntegrityError):
        await AssessmentConsumer(session_factory, assessor).consume(target)
    await _assert_rolled_back_with_failure(db_session)


@pytest.mark.asyncio
async def test_ready_read_db_failure_keeps_article_unlinked(
    db_session, test_database_url, target, assessor
):
    """状態取得のDB障害でもイベントの記事IDへfallbackせず失敗監査を残す。"""
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import create_async_engine

    await db_session.execute(
        text("LOCK TABLE article_curations IN ACCESS EXCLUSIVE MODE")
    )
    engine = create_async_engine(
        test_database_url, connect_args={"server_settings": {"lock_timeout": "100ms"}}
    )
    try:
        with pytest.raises(DBAPIError) as raised:
            await AssessmentConsumer(
                async_sessionmaker(engine, expire_on_commit=False), assessor
            ).consume(target)
        assert type(raised.value) is DBAPIError
        assert raised.value.orig.sqlstate == "55P03"
        assessor.assess.assert_not_awaited()
        events = await _events(db_session)
        assert len(events) == 1
        assert events[0].article_id is None
        assert events[0].outcome_code == "db_unknown_error"
    finally:
        await db_session.rollback()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("secondary_failure", ["none", "log", "metric"])
async def test_rejection_audit_failure_preserves_receipt_completion(
    db_session, session_factory, target, assessor, secondary_failure
):
    """拒否監査の保存と診断に失敗しても、確定した受信完了を維持する。"""
    from app.audit.stages.assessment import AssessmentAuditRepository

    event = target.model_copy(update={"curation_id": 999_999})
    append = AssessmentAuditRepository.append_ready_build_rejected

    async def append_then_fail(repo, **kwargs):
        await append(repo, **kwargs)
        raise RuntimeError("private-audit-details")

    with (
        patch.object(
            AssessmentAuditRepository,
            "append_ready_build_rejected",
            new=append_then_fail,
        ),
        patch(f"{_MODULE}_failure_handling.logger") as log,
        patch(f"{_MODULE}_failure_handling.record_audit_dropped") as dropped,
    ):
        if secondary_failure == "log":
            log.warning.side_effect = RuntimeError("private-log-details")
        if secondary_failure == "metric":
            dropped.side_effect = RuntimeError("private-metric-details")
        result = await AssessmentConsumer(session_factory, assessor).consume(event)

    assert result == AssessmentReadyBuildRejected(
        AssessmentReadyBuildRejectionReason.CURATION_MISSING
    )
    assert await _events(db_session) == []
    assessor.assess.assert_not_awaited()
    dropped.assert_called_once()
    assert "private" not in str(log.warning.call_args)


@pytest.mark.asyncio
async def test_rejection_handling_runs_after_business_timeout(
    session_factory, assessor
):
    """拒否確定後の監査は業務タイマーを解除してから実行する。"""
    consumer = AssessmentConsumer(session_factory, assessor)
    event = ArticleCuratedSignal(curation_id=999_999, analyzable_article_id=999_999)
    business_timeout = asyncio.timeout(60)
    handle_rejected = consumer._failure_handler.handle_ready_build_rejected

    async def handle_after_deadline(**kwargs):
        with pytest.raises(RuntimeError, match="finished"):
            business_timeout.reschedule(asyncio.get_running_loop().time())
        await asyncio.sleep(0)
        await handle_rejected(**kwargs)

    with (
        patch(f"{_MODULE}.timeout", return_value=business_timeout),
        patch.object(
            consumer._failure_handler,
            "handle_ready_build_rejected",
            side_effect=handle_after_deadline,
        ) as rejection_handler,
        patch.object(consumer._failure_handler, "handle") as failure_handler,
    ):
        result = await consumer.consume(event)

    assert isinstance(result, AssessmentReadyBuildRejected)
    rejection_handler.assert_awaited_once()
    failure_handler.assert_not_called()
    assessor.assess.assert_not_awaited()
