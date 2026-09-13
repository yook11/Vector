"""共通記事完成イベントからのCurationと失敗境界を実DBで検証する。"""

from __future__ import annotations

import asyncio
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
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
    AIProviderUsageLimitExhaustedError,
)
from app.ai_providers.gemini.error_translator import GeminiContentRejectionReason
from app.analysis.curation.ai.base import BaseCurator
from app.analysis.curation.ai.envelope import CurationCall
from app.analysis.curation.consumer import CurationConsumer
from app.analysis.curation.domain import Noise, Signal
from app.analysis.curation.domain.ready import (
    CurationReadyBuildRejected,
    CurationReadyBuildRejectionReason,
)
from app.analysis.curation.errors import CurationError, CurationResponseInvalidError
from app.analysis.curation.events import ArticleCuratedSignal
from app.analysis.curation.service import CurationCompletion, CurationCompletionKind
from app.collection.events import AnalyzableArticleCreated
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.curation_noise import CurationNoise
from app.models.outbox_event import OutboxEvent
from app.models.pipeline_event import PipelineEvent
from tests.cloudwatch.records import metric_records
from tests.outbox import RejectOutboxInsert

_MODULE = "app.analysis.curation.consumer"


def _call(kind="signal"):
    return CurationCall(
        result=Signal(title_ja="タイトル", summary_ja="要約")
        if kind == "signal"
        else Noise(title_ja="タイトル", summary_ja="要約"),
        raw_response='{"relevance":"' + kind + '"}',
        raw_relevance=kind,
        prompt_version="testver1",
        model_name="test-model",
    )


@pytest.fixture
def curator():
    fake = MagicMock(spec=BaseCurator)
    fake.provider = "gemini"
    fake.curate = AsyncMock(return_value=_call())
    return fake


@pytest.fixture
async def target(db_session, sample_source):
    article = AnalyzableArticleRecord(
        source_id=sample_source.id,
        source_url="https://example.com/curation-consumer",
        original_title="title",
        original_content="content",
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    return AnalyzableArticleCreated(analyzable_article_id=article.id)


async def _rows(session, model):
    return list((await session.scalars(select(model))).all())


async def _events(session):
    return list(
        (await session.scalars(select(PipelineEvent).order_by(PipelineEvent.id))).all()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["signal", "noise"])
async def test_completion_matches_committed_result_and_outbox(
    db_session, session_factory, target, curator, kind
):
    """Consumerの保存完了は実DBの結果・成功監査・必要なOutboxと一致する。"""
    curator.curate.return_value = _call(kind)
    result = await CurationConsumer(session_factory, curator).consume(target)
    records = await _rows(
        db_session, ArticleCuration if kind == "signal" else CurationNoise
    )
    assert len(records) == 1
    assert result == (
        CurationCompletion(CurationCompletionKind.SIGNAL, records[0].id)
        if kind == "signal"
        else CurationCompletion(CurationCompletionKind.NOISE)
    )
    audits = await _events(db_session)
    assert len(audits) == 1
    assert (audits[0].event_type, audits[0].outcome_code, audits[0].article_id) == (
        "succeeded",
        "curated_" + kind,
        target.analyzable_article_id,
    )
    outbox = await _rows(db_session, OutboxEvent)
    if kind == "signal":
        assert len(outbox) == 1
        assert outbox[0].event_type == ArticleCuratedSignal.EVENT_TYPE
        assert outbox[0].payload == {
            "analyzable_article_id": target.analyzable_article_id,
            "curation_id": records[0].id,
        }
    else:
        assert outbox == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["signal", "noise"])
async def test_already_curated_does_not_repeat_ai_audit_or_metrics(
    db_session, session_factory, target, curator, capsys, kind
):
    """Signal・Noise保存済みのイベントは副作用を追加せず処理済みになる。"""
    curator.curate.return_value = _call(kind)
    consumer = CurationConsumer(session_factory, curator)
    await consumer.consume(target)
    capsys.readouterr()
    again = await consumer.consume(target)
    assert again == CurationCompletion(CurationCompletionKind.ALREADY_CURATED)
    curator.curate.assert_awaited_once_with(title="title", content="content")
    assert len(await _events(db_session)) == 1
    assert len(await _rows(db_session, OutboxEvent)) == (1 if kind == "signal" else 0)
    assert metric_records(capsys.readouterr().out, "processing_outcome") == []


@pytest.mark.asyncio
async def test_missing_article_returns_unlinked_rejection(
    db_session, session_factory, curator
):
    """存在しない記事は探索IDをpayloadだけに保持して拒否する。"""
    event = AnalyzableArticleCreated(analyzable_article_id=999_999)
    result = await CurationConsumer(session_factory, curator).consume(event)
    assert result == CurationReadyBuildRejected(
        CurationReadyBuildRejectionReason.ARTICLE_MISSING
    )
    (audit,) = await _events(db_session)
    assert (audit.article_id, audit.source_id, audit.event_type) == (
        None,
        None,
        "rejected",
    )
    assert audit.payload["target_article_id"] == event.analyzable_article_id
    curator.curate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content, reason, length",
    [
        pytest.param(
            "", CurationReadyBuildRejectionReason.INPUT_INVALID, None, id="empty"
        ),
        pytest.param(
            "秘" * 200_001,
            CurationReadyBuildRejectionReason.CONTENT_TOO_LARGE,
            200_001,
            id="too-large",
        ),
    ],
)
async def test_ready_rejection_keeps_article_without_execution_side_effects(
    db_session, session_factory, target, curator, capsys, content, reason, length
):
    """入力拒否では記事を保持し、安全な拒否監査だけを残す。"""
    article = await db_session.get(
        AnalyzableArticleRecord, target.analyzable_article_id
    )
    article.original_content = content
    await db_session.commit()
    capsys.readouterr()
    consumer = CurationConsumer(session_factory, curator)
    with patch.object(consumer._service, "execute") as execute:
        result = await consumer.consume(target)
    assert result == CurationReadyBuildRejected(
        reason, target.analyzable_article_id, length, 200_000 if length else None
    )
    execute.assert_not_called()
    curator.curate.assert_not_awaited()
    (audit,) = await _events(db_session)
    assert (audit.outcome_code, audit.article_id, audit.event_type) == (
        reason.value,
        article.id,
        "rejected",
    )
    assert audit.payload["input_content_length"] == length
    assert audit.payload["max_content_length"] == result.max_content_length
    assert "秘" not in repr(audit.payload)
    for key in (
        "input_content_head",
        "ai_raw_response",
        "error_message",
        "error_chain",
    ):
        assert audit.payload[key] is None
    for model in (ArticleCuration, CurationNoise, OutboxEvent):
        assert await _rows(db_session, model) == []
    async with session_factory() as reader:
        assert await reader.get(AnalyzableArticleRecord, article.id) is not None
    output = capsys.readouterr().out
    assert metric_records(output, "processing_outcome") == []
    assert metric_records(output, "ai_provider_exhausted") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider",
    [
        AIProviderNetworkError(),
        AIProviderConfigurationError(),
        AIProviderUsageLimitExhaustedError(),
        AIProviderInputRejectedError(reason=GeminiContentRejectionReason.SAFETY),
        AIProviderOutputBlockedError(reason=GeminiContentRejectionReason.SAFETY),
    ],
)
async def test_provider_failure_propagates_without_deleting_article(
    db_session, session_factory, target, curator, provider
):
    """再試行可否やコンテンツ拒否にかかわらず記事を保持して元の原因を伝える。"""
    curator.curate.side_effect = provider
    with pytest.raises(CurationError) as raised:
        await CurationConsumer(session_factory, curator).consume(target)
    assert raised.value.provider_error is provider
    assert raised.value.__cause__ is provider
    (audit,) = await _events(db_session)
    assert audit.outcome_code == provider.CODE
    assert audit.payload["failure_action"] is None
    assert audit.article_id == target.analyzable_article_id
    assert (
        await db_session.get(AnalyzableArticleRecord, target.analyzable_article_id)
        is not None
    )
    for model in (ArticleCuration, CurationNoise, OutboxEvent):
        assert await _rows(db_session, model) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original", [CurationResponseInvalidError(), RuntimeError("unexpected")]
)
async def test_service_error_keeps_same_instance(
    session_factory, target, curator, original
):
    """Serviceの非プロバイダー例外は同じインスタンスで呼び出し元へ伝える。"""
    curator.curate.side_effect = original
    with pytest.raises(type(original)) as raised:
        await CurationConsumer(session_factory, curator).consume(target)
    assert raised.value is original


@pytest.mark.asyncio
async def test_ready_read_once_and_connection_returned_before_ai(
    session_factory, target, curator
):
    """一度取得したReady事実を使い、AI待機中に取得接続を占有しない。"""
    engine = session_factory.kw["bind"]
    reads = []
    borrowed = set()

    def checkout(connection, record, proxy):
        borrowed.add(id(record))

    def checkin(connection, record):
        borrowed.discard(id(record))

    def record_read(conn, cursor, statement, parameters, context, executemany):
        if "left outer join article_curations" in statement.lower():
            reads.append(statement)

    async def check_connection(**kwargs):
        assert borrowed == set()
        return _call()

    curator.curate.side_effect = check_connection
    sqlalchemy_event.listen(engine.sync_engine, "before_cursor_execute", record_read)
    sqlalchemy_event.listen(engine.sync_engine, "checkout", checkout)
    sqlalchemy_event.listen(engine.sync_engine, "checkin", checkin)
    try:
        await CurationConsumer(session_factory, curator).consume(target)
        assert len(reads) == 1
    finally:
        sqlalchemy_event.remove(
            engine.sync_engine, "before_cursor_execute", record_read
        )


async def _assert_rolled_back_with_failure(db_session):
    for model in (ArticleCuration, CurationNoise, OutboxEvent):
        assert await _rows(db_session, model) == []
    audits = await _events(db_session)
    assert len(audits) == 1 and audits[0].event_type == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["read", "ai", "save"])
async def test_timeout_covers_business_but_not_failure_handling(
    db_session, session_factory, target, curator, phase
):
    """状態取得・AI・保存の期限切れ後も、期限外で失敗監査を完了できる。"""
    if phase == "read":
        await db_session.execute(
            text("LOCK TABLE analyzable_articles IN ACCESS EXCLUSIVE MODE")
        )
    elif phase == "ai":

        async def wait_forever(**kwargs):
            await asyncio.Event().wait()

        curator.curate.side_effect = wait_forever
    else:

        async def lock_before_save(**kwargs):
            await db_session.execute(
                text("LOCK TABLE article_curations IN ACCESS EXCLUSIVE MODE")
            )
            return _call()

        curator.curate.side_effect = lock_before_save
    consumer = CurationConsumer(session_factory, curator)
    handle = consumer._failure_handler.handle

    async def slow_handler(**kwargs):
        # 監査も記事テーブルを参照するため、障害注入用ロックを先に解除する。
        await db_session.rollback()
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
            curator.curate.assert_not_awaited()
    finally:
        await db_session.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["classification", "handler"])
@pytest.mark.parametrize("broken_logger", [False, True])
async def test_secondary_failure_preserves_original(
    session_factory, target, curator, operation, broken_logger
):
    """分類・後処理・診断が壊れても最初の処理例外を再送出する。"""
    original = CurationResponseInvalidError()
    curator.curate.side_effect = original
    consumer = CurationConsumer(session_factory, curator)
    boundary = (
        patch(
            f"{_MODULE}.classify_curation_failure",
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
    db_session, session_factory, target, curator
):
    """外部キャンセルは業務失敗に変換せずそのまま伝播する。"""
    started = asyncio.Event()

    async def wait_cancel(**kwargs):
        started.set()
        await asyncio.Event().wait()

    curator.curate.side_effect = wait_cancel
    task = asyncio.create_task(
        CurationConsumer(session_factory, curator).consume(target)
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
async def test_outbox_failure_rolls_back_and_records_failure(
    db_session,
    session_factory,
    target,
    curator,
    reject_outbox_insert: RejectOutboxInsert,
):
    """実DBでOutboxを拒否すると保存・成功監査も戻り、失敗監査が残る。"""
    await reject_outbox_insert(ArticleCuratedSignal.EVENT_TYPE)
    with pytest.raises(IntegrityError):
        await CurationConsumer(session_factory, curator).consume(target)
    async with session_factory() as reader:
        await _assert_rolled_back_with_failure(reader)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["signal", "noise"])
async def test_commit_failure_rolls_back_and_uses_separate_failure_transaction(
    db_session, session_factory, target, curator, kind
):
    """保存commitの障害後も別セッションで失敗監査をcommitする。"""
    curator.curate.return_value = _call(kind)
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
        await CurationConsumer(factory, curator).consume(target)
    assert raised.value is original
    assert len(commits) == 2 and commits[0] is not commits[1]
    await _assert_rolled_back_with_failure(db_session)


@pytest.mark.asyncio
async def test_success_audit_failure_rolls_back_and_records_failure(
    db_session, session_factory, target, curator
):
    """成功監査の主キー違反で保存を戻し、失敗監査だけを追加する。"""
    baseline = PipelineEvent(
        id=1,
        stage="curation",
        event_type="succeeded",
        outcome_code="baseline",
        payload={},
    )
    db_session.add(baseline)
    await db_session.commit()
    with pytest.raises(IntegrityError):
        await CurationConsumer(session_factory, curator).consume(target)
    assert await _rows(db_session, ArticleCuration) == []
    assert await _rows(db_session, CurationNoise) == []
    assert await _rows(db_session, OutboxEvent) == []
    events = await _events(db_session)
    assert len(events) == 2 and events[1].event_type == "failed"


@pytest.mark.asyncio
async def test_ready_read_db_failure_keeps_article_unlinked(
    db_session, test_database_url, target, curator
):
    """状態取得のDB障害でもイベントの記事IDへfallbackせず失敗監査を残す。"""
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import create_async_engine

    await db_session.execute(
        text("LOCK TABLE analyzable_articles IN ACCESS EXCLUSIVE MODE")
    )
    engine = create_async_engine(
        test_database_url, connect_args={"server_settings": {"lock_timeout": "100ms"}}
    )
    consumer = CurationConsumer(
        async_sessionmaker(engine, expire_on_commit=False), curator
    )
    handle = consumer._failure_handler.handle

    async def unlock_then_handle(**kwargs):
        await db_session.rollback()
        await handle(**kwargs)

    try:
        with patch.object(
            consumer._failure_handler, "handle", side_effect=unlock_then_handle
        ):
            with pytest.raises(DBAPIError) as raised:
                await consumer.consume(target)
        assert type(raised.value) is DBAPIError
        assert raised.value.orig.sqlstate == "55P03"
        curator.curate.assert_not_awaited()
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
    db_session, session_factory, target, curator, secondary_failure
):
    """拒否監査の保存と診断に失敗しても、確定した受信完了を維持する。"""
    from app.audit.stages.curation import CurationAuditRepository

    event = target.model_copy(update={"analyzable_article_id": 999_999})
    append = CurationAuditRepository.append_ready_build_rejected

    async def append_then_fail(repo, **kwargs):
        await append(repo, **kwargs)
        raise RuntimeError("private-audit-details")

    with (
        patch.object(
            CurationAuditRepository,
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
        result = await CurationConsumer(session_factory, curator).consume(event)

    assert result == CurationReadyBuildRejected(
        CurationReadyBuildRejectionReason.ARTICLE_MISSING
    )
    assert await _events(db_session) == []
    curator.curate.assert_not_awaited()
    dropped.assert_called_once()
    assert "private" not in str(log.warning.call_args)


@pytest.mark.asyncio
async def test_rejection_handling_runs_after_business_timeout(session_factory, curator):
    """拒否確定後の監査は業務タイマーを解除してから実行する。"""
    consumer = CurationConsumer(session_factory, curator)
    event = AnalyzableArticleCreated(analyzable_article_id=999_999)
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

    assert isinstance(result, CurationReadyBuildRejected)
    rejection_handler.assert_awaited_once()
    assert rejection_handler.await_args.kwargs["rejected"] is result
    failure_handler.assert_not_called()
    curator.curate.assert_not_awaited()


@pytest.mark.asyncio
async def test_deletion_during_ai_preserves_save_failure_even_when_audit_drops(
    db_session, session_factory, target, curator, capfire
):
    """AI中の対象削除で保存と監査のFKが失敗しても、元の保存エラーを伝える。"""

    async def delete_article(**kwargs):
        async with session_factory() as session:
            await session.execute(
                delete(AnalyzableArticleRecord).where(
                    AnalyzableArticleRecord.id == target.analyzable_article_id
                )
            )
            await session.commit()
        return _call()

    curator.curate.side_effect = delete_article
    consumer = CurationConsumer(session_factory, curator)
    with patch.object(
        consumer._failure_handler, "handle", wraps=consumer._failure_handler.handle
    ) as handle:
        with pytest.raises(IntegrityError) as raised:
            await consumer.consume(target)
    assert handle.await_args.kwargs["exc"] is raised.value
    for model in (ArticleCuration, CurationNoise, OutboxEvent, PipelineEvent):
        assert await _rows(db_session, model) == []
    metric = next(
        m
        for m in capfire.get_collected_metrics()
        if m["name"] == "vector.audit.dropped"
    )
    assert metric["data"]["data_points"][0]["value"] == 1


@pytest.mark.asyncio
async def test_concurrent_consumers_return_saved_and_already_curated(
    db_session, session_factory, target, curator
):
    """Ready成立後の保存競合は一件だけを保存し、競合側を処理済みで返す。"""
    both_started = asyncio.Event()
    started = 0

    async def synchronize_ai(**kwargs):
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        async with asyncio.timeout(5):
            await both_started.wait()
        return _call()

    curator.curate.side_effect = synchronize_ai
    results = await asyncio.gather(
        CurationConsumer(session_factory, curator).consume(target),
        CurationConsumer(session_factory, curator).consume(target),
    )
    assert {result.kind for result in results} == {
        CurationCompletionKind.SIGNAL,
        CurationCompletionKind.ALREADY_CURATED,
    }
    assert len(await _rows(db_session, ArticleCuration)) == 1
    assert len(await _rows(db_session, OutboxEvent)) == 1
    assert len(await _events(db_session)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["ready_rejection", "execution_failure"])
async def test_audit_cancellation_propagates(
    db_session, session_factory, target, curator, phase
):
    """拒否監査・失敗監査へのキャンセルは通常の記録障害として抑止しない。"""
    from app.audit.stages.curation import CurationAuditRepository

    if phase == "ready_rejection":
        event = AnalyzableArticleCreated(analyzable_article_id=999_999)
        method = "append_ready_build_rejected"
    else:
        event = target
        curator.curate.side_effect = CurationResponseInvalidError()
        method = "append_classified_failure"
    cancelled = asyncio.CancelledError()
    with patch.object(CurationAuditRepository, method, side_effect=cancelled):
        with pytest.raises(asyncio.CancelledError) as raised:
            await CurationConsumer(session_factory, curator).consume(event)
    assert raised.value is cancelled
    assert await _events(db_session) == []
