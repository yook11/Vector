"""再投入の受付結果・バッチ分割・イベント契約をDBなしで検証する。"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from app.analysis.assessment.events import (
    ArticleAssessedInScope,
    ArticleAssessedInScopeEvent,
)
from app.analysis.curation.events import ArticleCuratedSignal, ArticleCuratedSignalEvent
from app.aws.sqs.errors import MessageSendUnexpectedError
from app.backfill import service
from app.backfill.targets import BackfillEventTarget
from app.collection.article_acquisition.events import (
    IncompleteArticleRecorded,
    IncompleteArticleRecordedEvent,
)
from app.collection.events import (
    AnalyzableArticleCreated,
    AnalyzableArticleCreatedEvent,
)
from app.outbox.publishing.analyzable_created import build_analyzable_created_message
from app.outbox.publishing.assessed_in_scope import build_assessed_in_scope_message
from app.outbox.publishing.contracts import (
    PublishFailed,
    PublishSucceeded,
)
from app.outbox.publishing.curated_signal import build_curated_signal_message
from app.outbox.publishing.incomplete_recorded import build_incomplete_recorded_message
from app.outbox.publishing.route import EventDeliveryRoute
from tests.backfill.helpers import delivery_arguments

NOW = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)


def targets(count):
    return [
        BackfillEventTarget(
            occurred_at=NOW,
            payload=AnalyzableArticleCreated(analyzable_article_id=i),
        )
        for i in range(1, count + 1)
    ]


async def dispatch(sender, items):
    await service._dispatch(
        sender, items, **delivery_arguments("curation"), stage="curation"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    [
        service.backfill_curations,
        service.backfill_assessments,
        service.backfill_embeddings,
        service.backfill_completions,
    ],
)
async def test_disabled_entry_never_opens_resources(entry):
    """無効ならDB・送信先に触れず正常終了する。"""
    factory, sender = Mock(), Mock()
    assert (
        await entry(
            factory, sender, **delivery_arguments("curation"), enabled=False, now=NOW
        )
        is None
    )
    factory.assert_not_called()
    sender.send_batch.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_splits_batches_and_preserves_order():
    """10件ずつ送信し、末尾の端数も入力順を保つ。"""
    sender = Mock()
    sender.send_batch.side_effect = lambda *, queue_url, batch: tuple(
        PublishSucceeded(message.event_id) for message in batch.messages
    )
    await dispatch(sender, targets(23))
    batches = [
        call.kwargs["batch"].messages for call in sender.send_batch.call_args_list
    ]
    assert [len(batch) for batch in batches] == [10, 10, 3]
    assert [
        json.loads(item.body)["payload"]["analyzable_article_id"]
        for batch in batches
        for item in batch
    ] == list(range(1, 24))


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_ids", [{2}, set(range(1, 24))], ids=["partial", "all"])
async def test_failed_items_do_not_stop_later_batches(failed_ids):
    """受付失敗があっても、残りのバッチを一度ずつ送信する。"""
    error = MessageSendUnexpectedError(exception_type="RuntimeError")
    sender = Mock()
    sender.send_batch.side_effect = lambda *, queue_url, batch: tuple(
        PublishFailed(item.event_id, error)
        if json.loads(item.body)["payload"]["analyzable_article_id"] in failed_ids
        else PublishSucceeded(item.event_id)
        for item in batch.messages
    )
    await dispatch(sender, targets(23))
    assert sender.send_batch.call_count == 3


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
        (
            IncompleteArticleRecorded(incomplete_article_id=11, source_id=22),
            build_incomplete_recorded_message,
            IncompleteArticleRecordedEvent,
        ),
    ],
)
async def test_reconstructed_event_is_accepted_by_consumer_contract(
    payload, builder, event_model
):
    """実際の本文生成関数で作ったメッセージをconsumerと共有する型で受理できる。"""
    sender = Mock()
    sender.send_batch.side_effect = lambda *, queue_url, batch: tuple(
        PublishSucceeded(message.event_id) for message in batch.messages
    )
    await service._dispatch(
        sender,
        [BackfillEventTarget(NOW, payload)],
        route=EventDeliveryRoute(payload.EVENT_TYPE, "queue-url"),
        build_message=builder,
        stage="test",
    )
    body = sender.send_batch.call_args.kwargs["batch"].messages[0].body
    event = event_model.from_input(json.loads(body))
    assert event.payload == payload
    assert event.occurred_at == NOW


@pytest.mark.asyncio
async def test_empty_targets_do_not_publish():
    """空のバッチは送信APIへ渡さない。"""
    sender = Mock()
    await dispatch(sender, [])
    sender.send_batch.assert_not_called()


@pytest.mark.asyncio
async def test_batch_preserves_built_messages():
    """本文生成で得たメッセージを、そのまま入力順でバッチにしてSenderへ渡す。"""
    built = []

    def build_message(envelope):
        message = build_analyzable_created_message(envelope)
        built.append(message)
        return message

    sender = Mock()
    sender.send_batch.side_effect = lambda *, queue_url, batch: tuple(
        PublishSucceeded(message.event_id) for message in batch.messages
    )
    await service._dispatch(
        sender,
        targets(2),
        route=delivery_arguments("curation")["route"],
        build_message=build_message,
        stage="curation",
    )

    batch = sender.send_batch.call_args.kwargs["batch"]
    for message, sent_message in zip(built, batch.messages, strict=True):
        assert sent_message is message


@pytest.mark.asyncio
async def test_all_conversion_failures_skip_send_and_continue_next_batch(monkeypatch):
    """全件変換失敗のバッチは送らず、次の正常バッチの受付件数だけを集計する。"""
    sender = Mock()
    sender.send_batch.side_effect = lambda *, queue_url, batch: tuple(
        PublishSucceeded(message.event_id) for message in batch.messages
    )
    recorded = Mock()
    monkeypatch.setattr(service, "record_dispatched", recorded)

    def build_message(envelope):
        if envelope.payload["analyzable_article_id"] <= 10:
            envelope = replace(envelope, schema_version=2)
        return build_analyzable_created_message(envelope)

    await service._dispatch(
        sender,
        targets(12),
        route=delivery_arguments("curation")["route"],
        build_message=build_message,
        stage="curation",
    )
    sender.send_batch.assert_called_once()
    assert [
        json.loads(m.body)["payload"]["analyzable_article_id"]
        for m in sender.send_batch.call_args.kwargs["batch"].messages
    ] == [11, 12]
    assert [call.args for call in recorded.call_args_list] == [
        ("curation", 0),
        ("curation", 2),
    ]


@pytest.mark.asyncio
async def test_conversion_failure_and_send_failure_count_only_success(monkeypatch):
    """変換失敗と送信失敗が同居しても、送信対象の順序と成功件数を保つ。"""
    sender = Mock()
    error = MessageSendUnexpectedError(exception_type="RuntimeError")

    def send(*, queue_url, batch):
        assert [
            json.loads(m.body)["payload"]["analyzable_article_id"]
            for m in batch.messages
        ] == [2, 3]
        return (
            PublishFailed(batch.messages[0].event_id, error),
            PublishSucceeded(batch.messages[1].event_id),
        )

    sender.send_batch.side_effect = send
    recorded = Mock()
    monkeypatch.setattr(service, "record_dispatched", recorded)

    def build_message(envelope):
        if envelope.payload["analyzable_article_id"] == 1:
            envelope = replace(envelope, schema_version=2)
        return build_analyzable_created_message(envelope)

    await service._dispatch(
        sender,
        targets(3),
        route=delivery_arguments("curation")["route"],
        build_message=build_message,
        stage="curation",
    )
    recorded.assert_called_once_with("curation", 1)


@pytest.mark.asyncio
async def test_unexpected_conversion_error_prevents_entire_batch_send():
    """途中の想定外の変換例外は、先行する正常メッセージも後続バッチも送らず伝播する。"""
    sender = Mock()
    error = RuntimeError("unexpected conversion")

    def build_message(envelope):
        if envelope.payload["analyzable_article_id"] == 2:
            raise error
        return build_analyzable_created_message(envelope)

    with pytest.raises(RuntimeError) as caught:
        await service._dispatch(
            sender,
            targets(12),
            route=delivery_arguments("curation")["route"],
            build_message=build_message,
            stage="curation",
        )
    assert caught.value is error
    sender.send_batch.assert_not_called()


@pytest.mark.asyncio
async def test_unexpected_sender_error_prevents_later_batches():
    """Senderの想定外例外をそのまま伝播し、後続バッチは送らない。"""
    error = RuntimeError("unexpected send")
    sender = Mock(send_batch=Mock(side_effect=error))
    with pytest.raises(RuntimeError) as caught:
        await dispatch(sender, targets(12))
    assert caught.value is error
    sender.send_batch.assert_called_once()


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
