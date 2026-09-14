"""再投入の受付結果・バッチ分割・イベント契約をDBなしで検証する。"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.analysis.assessment.events import (
    ArticleAssessedInScope,
    ArticleAssessedInScopeEvent,
)
from app.analysis.curation.events import ArticleCuratedSignal, ArticleCuratedSignalEvent
from app.backfill import service
from app.backfill.targets import BackfillEventTarget, BackfillTarget
from app.collection.events import (
    AnalyzableArticleCreated,
    AnalyzableArticleCreatedEvent,
)
from app.outbox.publishing.analyzable_created import build_analyzable_created_message
from app.outbox.publishing.assessed_in_scope import build_assessed_in_scope_message
from app.outbox.publishing.curated_signal import build_curated_signal_message
from app.outbox.publishing.errors import PublishError
from app.outbox.publishing.publisher import (
    BatchPublishResult,
    PublishFailed,
    PublishSucceeded,
)
from app.outbox.publishing.route import EventDeliveryRoute
from app.outbox.publishing.routed_publisher import RoutedEventPublisher

NOW = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)


def targets(count):
    return [
        BackfillEventTarget(
            target=BackfillTarget(i, i, "source"),
            occurred_at=NOW,
            payload=AnalyzableArticleCreated(analyzable_article_id=i),
        )
        for i in range(1, count + 1)
    ]


async def dispatch(publisher, items):
    await service._dispatch(
        Mock(),
        publisher,
        items,
        backfill_stage="curate",
        stage="curation",
        target_kind="article",
        run_id="run",
    )


@pytest.fixture
def audit(monkeypatch):
    recorder = AsyncMock()
    monkeypatch.setattr(service, "append_backfill_item_event", recorder)
    return recorder


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    [
        service.backfill_curations,
        service.backfill_assessments,
        service.backfill_embeddings,
    ],
)
async def test_disabled_entry_never_opens_resources(entry):
    """無効ならDB・送信先に触れず正常終了する。"""
    factory, publisher = Mock(), Mock()
    assert await entry(factory, publisher, enabled=False, now=NOW) is None
    factory.assert_not_called()
    publisher.publish_batch.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_splits_batches_and_preserves_order(audit):
    """10件ずつ送信し、末尾の端数も入力順を保つ。"""
    publisher = Mock()
    publisher.publish_batch.side_effect = lambda items: BatchPublishResult(
        tuple(PublishSucceeded(item.event_id) for item in items)
    )
    await dispatch(publisher, targets(23))
    batches = [call.args[0] for call in publisher.publish_batch.call_args_list]
    assert [len(batch) for batch in batches] == [10, 10, 3]
    assert [
        item.payload["analyzable_article_id"] for batch in batches for item in batch
    ] == list(range(1, 24))


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_ids", [{2}, set(range(1, 24))], ids=["partial", "all"])
async def test_failed_items_do_not_stop_later_batches(audit, failed_ids):
    """受付失敗を項目別に記録して、残りを一度ずつ送信する。"""
    error = PublishError()
    publisher = Mock()
    publisher.publish_batch.side_effect = lambda items: BatchPublishResult(
        tuple(
            PublishFailed(item.event_id, error)
            if item.payload["analyzable_article_id"] in failed_ids
            else PublishSucceeded(item.event_id)
            for item in items
        )
    )
    await dispatch(publisher, targets(23))
    assert publisher.publish_batch.call_count == 3
    failed = [
        call.kwargs["target"].target_id
        for call in audit.await_args_list
        if call.kwargs["exc"] is error
    ]
    assert set(failed) == failed_ids
    assert audit.await_count == 23


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["type", "missing", "extra", "reversed", "unknown", "duplicate"]
)
async def test_invalid_result_correspondence_is_rejected_before_audit(audit, fault):
    """不正な応答を別記事の結果として監査へ書かない。"""

    def respond(items):
        values = [PublishSucceeded(item.event_id) for item in items]
        if fault == "type":
            return None
        if fault == "missing":
            values.pop()
        elif fault == "extra":
            values.append(PublishSucceeded(uuid4()))
        elif fault == "reversed":
            values.reverse()
        elif fault == "unknown":
            values[0] = PublishSucceeded(uuid4())
        elif fault == "duplicate":
            values[1] = values[0]
        return BatchPublishResult(tuple(values))

    publisher = Mock()
    publisher.publish_batch.side_effect = respond
    with pytest.raises((TypeError, ValueError)):
        await dispatch(publisher, targets(12))
    audit.assert_not_awaited()
    assert publisher.publish_batch.call_count == 1


@pytest.mark.asyncio
async def test_each_dispatch_generates_fresh_event_ids(audit):
    """同じ事実の再投入でも別のイベントIDを割り当てる。"""
    publisher = Mock()
    publisher.publish_batch.side_effect = lambda items: BatchPublishResult(
        tuple(PublishSucceeded(item.event_id) for item in items)
    )
    await dispatch(publisher, targets(1))
    await dispatch(publisher, targets(1))
    first, second = [call.args[0][0] for call in publisher.publish_batch.call_args_list]
    assert first.event_id != second.event_id
    assert first.payload == second.payload
    assert first.occurred_at == second.occurred_at == NOW


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "builder", "event_model"),
    [
        (
            AnalyzableArticleCreated(analyzable_article_id=11),
            build_analyzable_created_message,
            AnalyzableArticleCreatedEvent,
        ),
        (
            ArticleCuratedSignal(analyzable_article_id=11, curation_id=22),
            build_curated_signal_message,
            ArticleCuratedSignalEvent,
        ),
        (
            ArticleAssessedInScope(curation_id=22, analyzed_article_id=33),
            build_assessed_in_scope_message,
            ArticleAssessedInScopeEvent,
        ),
    ],
)
async def test_reconstructed_event_is_accepted_by_consumer_contract(
    audit, payload, builder, event_model
):
    """既存publisherが生成した本文をconsumerと共有する型で受理できる。"""
    sender = Mock()
    sender.send_batch.side_effect = lambda **kwargs: BatchPublishResult(
        tuple(PublishSucceeded(message.event_id) for message in kwargs["messages"])
    )
    publisher = RoutedEventPublisher(
        EventDeliveryRoute(payload.EVENT_TYPE, "queue-url", builder), sender
    )
    await dispatch(
        publisher, [BackfillEventTarget(BackfillTarget(11, 11, "source"), NOW, payload)]
    )
    body = sender.send_batch.call_args.kwargs["messages"][0].body
    event = event_model.from_input(json.loads(body))
    assert event.payload == payload
    assert event.occurred_at == NOW


@pytest.mark.asyncio
async def test_empty_targets_do_not_publish(audit):
    """空のバッチは送信APIへ渡さない。"""
    publisher = Mock()
    await dispatch(publisher, [])
    publisher.publish_batch.assert_not_called()


def test_backfill_imports_without_taskiq_initialization():
    """単独起動でも循環参照や旧queueの初期化を起こさない。"""
    import subprocess
    import sys

    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "import sys; import app.backfill.service; "
            "assert not any(name == 'app.queue' or "
            "name.startswith(('app.queue.', 'taskiq')) "
            "for name in sys.modules)",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
