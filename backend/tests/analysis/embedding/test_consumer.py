"""Consumerの開始判定・保存・失敗伝播を実DBで検証する。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select, text
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from structlog.testing import capture_logs

from app.analysis.ai_provider_errors import (
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
    AIProviderServiceUnavailableError,
    AIProviderUsageLimitExhaustedError,
)
from app.analysis.assessment.events import ArticleAssessedInScope
from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.consumer import EmbeddingConsumer
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.domain.value_objects import (
    EMBEDDING_DIMENSION,
    EmbeddingVector,
)
from app.analysis.embedding.errors import (
    EmbeddingAnalyzedArticleMissingError,
    EmbeddingError,
)
from app.analysis.embedding.service import EmbeddingCompletionReason
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.pipeline_event import PipelineEvent
from app.queue.messages.embedding import EmbeddingTrigger
from tests.cloudwatch.records import metric_records
from tests.lambda_handlers.embedding_fixtures import run_embedding as run_embedding

_MODULE = "app.analysis.embedding.consumer"


@pytest.fixture
def consumer(session_factory, embedder):
    return EmbeddingConsumer(session_factory, embedder)


@pytest.fixture
async def target(db_session, sample_source, sample_categories):
    article = AnalyzableArticleRecord(
        source_id=sample_source.id,
        source_url="https://example.com/consumer",
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
    await db_session.flush()
    analyzed = AnalyzedArticleRecord(
        curation_id=curation.id,
        translated_title="title",
        summary="summary",
        investor_take="take",
        category_id=sample_categories[0].id,
    )
    db_session.add(analyzed)
    await db_session.commit()
    return ArticleAssessedInScope(
        curation_id=curation.id, analyzed_article_id=analyzed.id
    ), article.id


@pytest.fixture
def embedder():
    fake = MagicMock(spec=BaseEmbedder)
    fake.provider = "gemini"
    fake.model_name = "gemini-embedding-001"
    fake.dimension = EMBEDDING_DIMENSION
    fake.embed_document = AsyncMock(
        return_value=EmbeddingVector(root=(0.2,) * EMBEDDING_DIMENSION)
    )
    return fake


async def _events(session):
    return list(
        (
            await session.execute(select(PipelineEvent).order_by(PipelineEvent.id))
        ).scalars()
    )


@pytest.mark.asyncio
async def test_saves_once_and_uses_analyzed_id_without_curation_lookup(
    db_session, session_factory, target, embedder, capsys
):
    """分析記事IDで保存し、イベントのcuration IDは再照合しない。"""
    event, article_id = target
    event = event.model_copy(update={"curation_id": 999_999})
    result = await EmbeddingConsumer(session_factory, embedder).consume(event)
    assert result.reason is EmbeddingCompletionReason.SAVED
    events = await _events(db_session)
    assert len(events) == 1
    assert events[0].event_type == "succeeded"
    assert events[0].article_id == article_id
    stored = await db_session.get(AnalyzedArticleRecord, event.analyzed_article_id)
    assert stored.embedding is not None
    embedder.embed_document.assert_awaited_once()
    outcomes = metric_records(capsys.readouterr().out, "processing_outcome")
    assert [r["result"] for r in outcomes] == ["succeeded"]


@pytest.mark.asyncio
async def test_already_embedded_completes_without_ai_or_audit(
    db_session, session_factory, target, embedder, capsys
):
    """開始時の生成済みはAI・監査・成功計測を追加しない。"""
    event, _ = target
    stored = await db_session.get(AnalyzedArticleRecord, event.analyzed_article_id)
    stored.embedding = [0.4] * EMBEDDING_DIMENSION
    await db_session.commit()
    result = await EmbeddingConsumer(session_factory, embedder).consume(event)
    assert result.reason is EmbeddingCompletionReason.ALREADY_EMBEDDED
    embedder.embed_document.assert_not_awaited()
    assert await _events(db_session) == []
    assert metric_records(capsys.readouterr().out, "processing_outcome") == []


@pytest.mark.asyncio
async def test_missing_at_start_records_unlinked_failure(
    db_session, session_factory, embedder
):
    """開始時の不存在でも分析記事IDを残して失敗監査を確定する。"""
    event = ArticleAssessedInScope(curation_id=999_999, analyzed_article_id=999_999)
    with pytest.raises(EmbeddingAnalyzedArticleMissingError):
        await EmbeddingConsumer(session_factory, embedder).consume(event)
    embedder.embed_document.assert_not_awaited()
    events = await _events(db_session)
    assert len(events) == 1
    assert events[0].article_id is None
    assert events[0].outcome_code == "embedding_analyzed_article_missing"
    assert events[0].payload["analyzed_article_id"] == event.analyzed_article_id
    assert events[0].payload["failure_kind"] == "target_missing"


@pytest.mark.asyncio
async def test_ready_validation_failure_keeps_known_article_id(
    db_session, session_factory, target, embedder
):
    """Readyの検証失敗でも取得済みの記事IDを失敗監査に使う。"""
    event, article_id = target
    with pytest.raises(ValidationError) as invalid:
        ReadyForEmbedding(
            analyzed_article_id=event.analyzed_article_id, text_for_embedding=""
        )
    with patch(f"{_MODULE}.ReadyForEmbedding.from_facts", side_effect=invalid.value):
        with pytest.raises(ValidationError) as raised:
            await EmbeddingConsumer(session_factory, embedder).consume(event)
    assert raised.value is invalid.value
    embedder.embed_document.assert_not_awaited()
    events = await _events(db_session)
    assert len(events) == 1
    assert events[0].article_id == article_id
    assert events[0].outcome_code == "unexpected_error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_error",
    [
        AIProviderNetworkError(),
        AIProviderRateLimitedError(),
        AIProviderServiceUnavailableError(),
        AIProviderInsufficientBalanceError(),
        AIProviderUsageLimitExhaustedError(),
    ],
)
async def test_provider_failure_is_audited_and_propagated(
    db_session, session_factory, target, embedder, provider_error
):
    """監査できてもAPI障害を正常完了に変えない。"""
    event, _ = target
    embedder.embed_document.side_effect = provider_error
    with pytest.raises(EmbeddingError) as raised:
        await EmbeddingConsumer(session_factory, embedder).consume(event)
    assert raised.value.provider_error is provider_error
    assert raised.value.__cause__ is provider_error
    events = await _events(db_session)
    assert len(events) == 1
    assert events[0].event_type == "failed"
    assert events[0].outcome_code == provider_error.CODE


@pytest.mark.asyncio
async def test_deleted_during_ai_is_failure(
    db_session, session_factory, target, embedder
):
    """AI待機中の削除は妨げず、保存先の不存在を失敗にする。"""
    event, article_id = target
    vector = embedder.embed_document.return_value

    async def delete_during_ai(_ready):
        async with session_factory() as other:
            await other.execute(
                delete(AnalyzedArticleRecord).where(
                    AnalyzedArticleRecord.id == event.analyzed_article_id
                )
            )
            await other.commit()
        return vector

    embedder.embed_document.side_effect = delete_during_ai
    async with asyncio.timeout(5):
        with pytest.raises(EmbeddingAnalyzedArticleMissingError):
            await EmbeddingConsumer(session_factory, embedder).consume(event)
    events = await _events(db_session)
    assert len(events) == 1
    assert events[0].event_type == "failed"
    assert events[0].article_id == article_id


@pytest.mark.asyncio
@pytest.mark.parametrize("peer", ["consumer", "taskiq"])
async def test_concurrent_runs_save_and_audit_once(
    db_session, session_factory, target, embedder, peer
):
    """Consumer同士および既存Taskiqとの並行実行で保存を一度に収める。"""
    from app.queue.tasks.embedding import generate_embedding

    event, article_id = target
    barrier = asyncio.Barrier(2)

    async def together(_ready):
        index = await barrier.wait()
        return EmbeddingVector(root=(0.2 + index * 0.4,) * EMBEDDING_DIMENSION)

    embedder.embed_document.side_effect = together
    consumer = EmbeddingConsumer(session_factory, embedder)
    if peer == "consumer":
        other = EmbeddingConsumer(session_factory, embedder).consume(event)
    else:
        ctx = SimpleNamespace(
            state=SimpleNamespace(session_factory=session_factory, embedder=embedder)
        )
        other = generate_embedding(
            trigger=EmbeddingTrigger(
                analyzed_article_id=event.analyzed_article_id,
                analyzable_article_id=article_id,
            ),
            ctx=ctx,
        )
    async with asyncio.timeout(10):
        results = await asyncio.gather(consumer.consume(event), other)
    if peer == "consumer":
        assert {result.reason for result in results} == {
            EmbeddingCompletionReason.SAVED,
            EmbeddingCompletionReason.ALREADY_EMBEDDED,
        }
    else:
        assert results[0].reason in (
            EmbeddingCompletionReason.SAVED,
            EmbeddingCompletionReason.ALREADY_EMBEDDED,
        )
    assert embedder.embed_document.await_count == 2
    events = await _events(db_session)
    assert len(events) == 1 and events[0].event_type == "succeeded"
    stored = await db_session.get(AnalyzedArticleRecord, event.analyzed_article_id)
    assert stored.embedding is not None


@pytest.mark.asyncio
async def test_commit_failure_rolls_back_vector_and_records_failure(
    db_session, session_factory, target, embedder
):
    """成功監査の実DB制約違反で保存を戻し、失敗監査だけを追加する。"""
    event, _ = target
    db_session.add(
        PipelineEvent(
            id=1,
            stage="embedding",
            event_type="succeeded",
            outcome_code="baseline",
            payload={},
        )
    )
    await db_session.commit()
    with pytest.raises(IntegrityError):
        await EmbeddingConsumer(session_factory, embedder).consume(event)
    stored = await db_session.get(AnalyzedArticleRecord, event.analyzed_article_id)
    assert stored.embedding is None
    events = await _events(db_session)
    assert len(events) == 2
    assert events[1].event_type == "failed"
    assert events[1].outcome_code == "db_constraint_error"


@pytest.mark.asyncio
async def test_db_lock_failure_propagates(
    db_session, test_database_url, target, embedder
):
    """保存時のDB待機失敗は監査後も元のDB例外として伝える。"""
    event, _ = target
    await db_session.execute(
        select(AnalyzedArticleRecord.id)
        .where(AnalyzedArticleRecord.id == event.analyzed_article_id)
        .with_for_update()
    )
    engine = create_async_engine(
        test_database_url, connect_args={"server_settings": {"lock_timeout": "100ms"}}
    )
    try:
        with pytest.raises(DBAPIError) as raised:
            await EmbeddingConsumer(
                async_sessionmaker(engine, expire_on_commit=False), embedder
            ).consume(event)
        assert raised.value.orig.sqlstate == "55P03"
        events = await _events(db_session)
        assert len(events) == 1 and events[0].event_type == "failed"
    finally:
        await db_session.rollback()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["read", "ai"])
async def test_timeout_covers_business_work_but_not_failure_handling(
    db_session, session_factory, target, embedder, phase
):
    """60秒の対象は取得からAIまでを含み、期限切れ後に実DB監査が完了する。"""
    event, article_id = target
    if phase == "read":
        await db_session.execute(
            text("LOCK TABLE analyzed_articles IN ACCESS EXCLUSIVE MODE")
        )
    else:

        async def wait_forever(_ready):
            await asyncio.Event().wait()

        embedder.embed_document.side_effect = wait_forever
    consumer = EmbeddingConsumer(session_factory, embedder)
    handle = consumer._failure_handler.handle

    async def slow_failure_handling(**kwargs):
        await asyncio.sleep(0.1)
        return await handle(**kwargs)

    with (
        patch(
            f"{_MODULE}.timeout", side_effect=lambda delay: asyncio.timeout(0.05)
        ) as timeout_factory,
        patch.object(
            consumer._failure_handler, "handle", side_effect=slow_failure_handling
        ),
    ):
        with pytest.raises(TimeoutError):
            await consumer.consume(event)
    timeout_factory.assert_called_once_with(60)
    events = await _events(db_session)
    assert len(events) == 1
    assert events[0].event_type == "failed"
    assert events[0].article_id == (article_id if phase == "ai" else None)
    if phase == "read":
        embedder.embed_document.assert_not_awaited()
        await db_session.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["classification", "handler"])
@pytest.mark.parametrize("broken_logger", [False, True])
async def test_secondary_failure_cannot_replace_service_error(
    session_factory, target, embedder, operation, broken_logger
):
    """分類・後処理・ログの障害があっても元のService例外を保持する。"""
    event, _ = target
    original = AIProviderNetworkError()
    embedder.embed_document.side_effect = original
    consumer = EmbeddingConsumer(session_factory, embedder)
    boundary = (
        patch(
            f"{_MODULE}.classify_embedding_failure",
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
            with patch(
                f"{_MODULE}.logger.warning", side_effect=RuntimeError("log-secret")
            ):
                with pytest.raises(EmbeddingError) as raised:
                    await consumer.consume(event)
        else:
            with pytest.raises(EmbeddingError) as raised:
                await consumer.consume(event)
    assert raised.value.provider_error is original
    assert raised.value.__cause__ is original
    assert "secondary-secret" not in str(logs)


@pytest.mark.asyncio
async def test_audit_failure_still_propagates_and_notifies(
    db_session, session_factory, target, embedder, capsys
):
    """元記事の削除で監査不能になっても、元の失敗と枯渇通知を維持する。"""
    event, article_id = target
    original = AIProviderUsageLimitExhaustedError()

    async def delete_parent(_ready):
        async with session_factory() as session:
            await session.execute(
                delete(AnalyzableArticleRecord).where(
                    AnalyzableArticleRecord.id == article_id
                )
            )
            await session.commit()
        raise original

    embedder.embed_document.side_effect = delete_parent
    with pytest.raises(EmbeddingError) as raised:
        await EmbeddingConsumer(session_factory, embedder).consume(event)
    assert raised.value.__cause__ is original
    assert await _events(db_session) == []
    assert len(metric_records(capsys.readouterr().out, "ai_provider_exhausted")) == 1


@pytest.mark.asyncio
async def test_cancellation_is_not_converted_to_business_failure(
    db_session, session_factory, target, embedder
):
    """外部キャンセルは失敗後処理へ変換せず伝播する。"""
    event, _ = target
    started = asyncio.Event()

    async def wait_for_cancel(_ready):
        started.set()
        await asyncio.Event().wait()

    embedder.embed_document.side_effect = wait_for_cancel
    task = asyncio.create_task(
        EmbeddingConsumer(session_factory, embedder).consume(event)
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
async def test_loads_once_and_closes_read_connection_before_ai(
    session_factory, target, embedder
):
    """開始状態は一度取得し、AI実行時には取得用接続を返却している。"""
    event, _ = target
    engine = session_factory.kw["bind"].sync_engine
    active = set()
    reads = []

    def checkout(connection, record, proxy):
        active.add(id(connection))

    def checkin(connection, record):
        active.discard(id(connection))

    def before_execute(conn, cursor, statement, parameters, context, executemany):
        if "join article_curations" in statement.lower():
            reads.append(statement)

    vector = embedder.embed_document.return_value

    async def verify_no_connection(_ready):
        assert not active
        return vector

    embedder.embed_document.side_effect = verify_no_connection
    listeners = [
        ("checkout", checkout),
        ("checkin", checkin),
        ("before_cursor_execute", before_execute),
    ]
    for name, callback in listeners:
        sqlalchemy_event.listen(engine, name, callback)
    try:
        result = await EmbeddingConsumer(session_factory, embedder).consume(event)
        assert result.reason is EmbeddingCompletionReason.SAVED
        assert len(reads) == 1
        assert not active
    finally:
        for name, callback in listeners:
            sqlalchemy_event.remove(engine, name, callback)


def _sqs_record(message_id, payload):
    from uuid import UUID

    from app.analysis.assessment.events import ArticleAssessedInScopeEvent
    from app.outbox.sqs.message import SqsMessage

    event = ArticleAssessedInScopeEvent(
        event_id=UUID(int=1),
        event_type=payload.EVENT_TYPE,
        schema_version=payload.SCHEMA_VERSION,
        occurred_at=datetime.now(UTC),
        payload=payload,
    )
    return {"messageId": message_id, "body": SqsMessage.from_event(event).body}


@pytest.mark.asyncio
async def test_sqs_processing_saves_once_and_records_only_business_failures(
    db_session, target, embedder, capsys, run_embedding
):
    """保存・生成済み・本文不正・記事不存在を実Consumerへ接続し監査を重ねない。"""
    payload, article_id = target
    missing = ArticleAssessedInScope(curation_id=999999, analyzed_article_id=999999)
    response = await run_embedding(
        {
            "Records": [
                _sqs_record("saved", payload),
                _sqs_record("already", payload),
                {"messageId": "invalid", "body": "not-json"},
                _sqs_record("missing", missing),
            ]
        },
    )
    assert response == {
        "batchItemFailures": [
            {"itemIdentifier": "invalid"},
            {"itemIdentifier": "missing"},
        ]
    }
    audits = await _events(db_session)
    assert [audit.event_type for audit in audits] == ["succeeded", "failed"]
    assert audits[0].article_id == article_id
    assert audits[1].outcome_code == "embedding_analyzed_article_missing"
    assert (
        await db_session.get(AnalyzedArticleRecord, payload.analyzed_article_id)
    ).embedding is not None
    embedder.embed_document.assert_awaited_once()
    outcomes = metric_records(capsys.readouterr().out, "processing_outcome")
    assert [r["result"] for r in outcomes] == ["succeeded", "failed"]


@pytest.mark.asyncio
async def test_sqs_processing_continues_after_provider_failure(
    db_session, target, embedder, run_embedding
):
    """API失敗を応答へ残し、次のメッセージの保存と監査を確定する。"""
    payload, _ = target
    embedder.embed_document.side_effect = [
        AIProviderNetworkError(),
        EmbeddingVector(root=(0.2,) * EMBEDDING_DIMENSION),
    ]
    response = await run_embedding(
        {"Records": [_sqs_record("failed", payload), _sqs_record("saved", payload)]},
    )
    assert response == {"batchItemFailures": [{"itemIdentifier": "failed"}]}
    assert [audit.event_type for audit in await _events(db_session)] == [
        "failed",
        "succeeded",
    ]
    assert embedder.embed_document.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["saved", "api_error", "invalid_vector"])
async def test_borrowed_gemini_embedder_through_consumer(
    db_session, session_factory, target, outcome
):
    """新しいEmbedderで保存・生成済み・失敗監査まで接続する。"""
    from google.genai import errors, types

    from app.analysis.embedding.embedder import GeminiEmbedder
    from app.analysis.embedding.errors import EmbeddingFailureReason

    api = AsyncMock(
        return_value=types.EmbedContentResponse(
            embeddings=[types.ContentEmbedding(values=[0.2] * EMBEDDING_DIMENSION)]
        )
    )
    if outcome == "api_error":
        api.side_effect = errors.ServerError(503, {"error": {"code": 503}})
    elif outcome == "invalid_vector":
        api.return_value = types.EmbedContentResponse(
            embeddings=[types.ContentEmbedding(values=[])]
        )
    sdk_client = SimpleNamespace(
        models=SimpleNamespace(embed_content=api), aclose=AsyncMock()
    )
    consumer = EmbeddingConsumer(session_factory, GeminiEmbedder(client=sdk_client))
    event, article_id = target
    if outcome == "saved":
        assert (await consumer.consume(event)).reason is EmbeddingCompletionReason.SAVED
        assert (
            await consumer.consume(event)
        ).reason is EmbeddingCompletionReason.ALREADY_EMBEDDED
    else:
        with pytest.raises(EmbeddingError) as caught:
            await consumer.consume(event)
        assert caught.value.reason is (
            EmbeddingFailureReason.PROVIDER_ERROR
            if outcome == "api_error"
            else EmbeddingFailureReason.RESPONSE_INVALID
        )
    api.assert_awaited_once()
    sdk_client.aclose.assert_not_called()
    audits = await _events(db_session)
    assert len(audits) == 1
    assert audits[0].article_id == article_id
    assert audits[0].payload["analyzed_article_id"] == event.analyzed_article_id
    assert audits[0].event_type == ("succeeded" if outcome == "saved" else "failed")
    stored = await db_session.get(AnalyzedArticleRecord, event.analyzed_article_id)
    if outcome == "saved":
        assert len(stored.embedding) == EMBEDDING_DIMENSION
        assert list(stored.embedding) == pytest.approx(
            [0.2] * EMBEDDING_DIMENSION, abs=0.001
        )
        assert audits[0].payload["ai_model"] == "gemini-embedding-001"
        assert audits[0].payload["vector_dimension"] == EMBEDDING_DIMENSION
    else:
        assert stored.embedding is None


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect", [False, True])
async def test_consumer_with_invocation_database_resources(
    db_session, test_database_url, target, embedder, monkeypatch, disconnect
):
    """呼び出し内のプールで保存し、切断後も失敗監査へ再接続する。"""
    from pydantic import SecretStr

    from app.lambda_handlers.embedding import resources as resources_module
    from app.lambda_handlers.embedding.settings import EmbeddingConsumerSettings

    monkeypatch.setattr(
        resources_module,
        "get_secret_parameter",
        lambda **kwargs: SecretStr("private"),
    )
    config = EmbeddingConsumerSettings(
        env="test",
        database_url=test_database_url,
        db_iam_auth=False,
        aws_region="ap-northeast-1",
        gemini_api_key_parameter_path="/key",
    )
    event, article_id = target
    disconnected = asyncio.Event()
    async with resources_module.open_embedding_resources(config) as resources:
        async with resources.session_factory() as session:
            original_pid = await session.scalar(text("select pg_backend_pid()"))
            connection = await session.connection()
            raw = await connection.get_raw_connection()
            raw.driver_connection.add_termination_listener(lambda _: disconnected.set())
        if disconnect:

            async def interrupt_ai(ready):
                await db_session.execute(
                    text("select pg_terminate_backend(:pid)"), {"pid": original_pid}
                )
                await asyncio.wait_for(disconnected.wait(), timeout=2)
                raise AIProviderNetworkError()

            embedder.embed_document.side_effect = interrupt_ai
        consumer = EmbeddingConsumer(resources.session_factory, embedder)
        if disconnect:
            with pytest.raises(EmbeddingError):
                await consumer.consume(event)
            async with resources.session_factory() as session:
                assert (
                    await session.scalar(text("select pg_backend_pid()"))
                    != original_pid
                )
        else:
            assert (
                await consumer.consume(event)
            ).reason is EmbeddingCompletionReason.SAVED
            assert (
                await consumer.consume(event)
            ).reason is EmbeddingCompletionReason.ALREADY_EMBEDDED
        embedder.embed_document.assert_awaited_once()
    events = await _events(db_session)
    assert len(events) == 1
    assert events[0].article_id == article_id
    assert events[0].event_type == ("failed" if disconnect else "succeeded")


@pytest.mark.asyncio
@pytest.mark.parametrize("api_fails_first", [False, True])
async def test_lambda_assembly_saves_once_after_individual_failure(
    db_session, test_database_url, target, monkeypatch, api_fails_first
):
    """実SDK・Consumer・DBを入口で組み立て、失敗後の保存と再処理を確認する。"""
    from unittest.mock import Mock

    import httpx
    from pydantic import SecretStr

    from app.ai_providers.gemini import client as gemini_module
    from app.lambda_handlers.embedding import resources as resource_module
    from app.lambda_handlers.embedding.handler import _run_embedding
    from app.lambda_handlers.embedding.settings import EmbeddingConsumerSettings

    settings = EmbeddingConsumerSettings(
        env="test",
        aws_region="ap-northeast-1",
        database_url=test_database_url,
        db_iam_auth=False,
        gemini_api_key_parameter_path="/test/gemini-key",
    )
    secret = Mock(return_value=SecretStr("test-private-key"))
    monkeypatch.setattr(resource_module, "get_secret_parameter", secret)
    requests, clients, engines = [], [], []
    create_engine = resource_module.create_embedding_consumer_engine

    def track_engine(*args, **kwargs):
        engine = create_engine(*args, **kwargs)
        engines.append(engine)
        return engine

    monkeypatch.setattr(
        resource_module, "create_embedding_consumer_engine", track_engine
    )

    def respond(request):
        requests.append(request)
        if api_fails_first and len(requests) == 1:
            return httpx.Response(
                503, json={"error": {"code": 503, "message": "unavailable"}}
            )
        return httpx.Response(
            200, json={"embeddings": [{"values": [0.2] * EMBEDDING_DIMENSION}]}
        )

    def http_factory(**kwargs):
        assert kwargs.pop("retries") == 0
        http = httpx.AsyncClient(transport=httpx.MockTransport(respond), **kwargs)
        clients.append(http)
        return http

    monkeypatch.setattr(gemini_module, "make_external_async_client", http_factory)
    payload, article_id = target
    records = [_sqs_record("saved", payload), _sqs_record("already", payload)]
    if api_fails_first:
        records.insert(0, _sqs_record("failed", payload))
    response = await _run_embedding({"Records": records}, settings)
    assert response == {
        "batchItemFailures": [{"itemIdentifier": "failed"}] if api_fails_first else []
    }
    assert await _run_embedding(
        {"Records": [_sqs_record("next-invocation", payload)]}, settings
    ) == {"batchItemFailures": []}
    assert secret.call_count == 2
    assert len(requests) == (2 if api_fails_first else 1)
    assert all(http.is_closed for http in clients)
    assert len(engines) == 2
    assert all(engine.pool.checkedin() == 0 for engine in engines)
    audits = await _events(db_session)
    assert [audit.event_type for audit in audits] == (
        ["failed", "succeeded"] if api_fails_first else ["succeeded"]
    )
    assert all(audit.article_id == article_id for audit in audits)
    stored = await db_session.get(AnalyzedArticleRecord, payload.analyzed_article_id)
    assert list(stored.embedding) == pytest.approx(
        [0.2] * EMBEDDING_DIMENSION, abs=0.001
    )
