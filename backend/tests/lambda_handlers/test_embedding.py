"""配送構造の事前確認と、メッセージ単位の応答・ログ境界を検証する。"""

import asyncio
import json
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from structlog.testing import capture_logs

from app.analysis.assessment.events import ArticleAssessedInScope
from app.analysis.embedding.consumer import EmbeddingConsumer
from app.analysis.embedding.service import (
    EmbeddingCompletion,
    EmbeddingCompletionReason,
)
from app.lambda_handlers import embedding as module
from app.lambda_handlers.embedding import (
    EmbeddingSqsInputError,
    process_embedding_messages,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def record(message_id="msg-1", article_id=1):
    return {
        "messageId": message_id,
        "body": json.dumps(
            {
                "event_id": str(UUID(int=article_id)),
                "event_type": "article.assessed_in_scope",
                "schema_version": 1,
                "occurred_at": "2026-09-10T00:00:00Z",
                "payload": {
                    "curation_id": article_id,
                    "analyzed_article_id": article_id,
                },
            }
        ),
    }


@pytest.fixture
def consumer():
    result = Mock(spec=EmbeddingConsumer)
    result.consume = AsyncMock(
        return_value=EmbeddingCompletion(EmbeddingCompletionReason.SAVED)
    )
    return result


@pytest.mark.parametrize(
    "records", [[], [record()], [record("msg-1", 1), record("msg-2", 2)]]
)
async def test_success_returns_consistent_response_and_preserves_input_order(
    consumer, records
):
    with capture_logs() as logs:
        response = await process_embedding_messages(
            {"Records": records, "unused": "ignored"}, consumer=consumer
        )
    assert response == {"batchItemFailures": []}
    assert [call.args[0] for call in consumer.consume.await_args_list] == [
        ArticleAssessedInScope(curation_id=i, analyzed_article_id=i)
        for i in range(1, len(records) + 1)
    ]
    assert [entry["message_id"] for entry in logs] == [r["messageId"] for r in records]
    assert all("event_id" in entry and "analyzed_article_id" in entry for entry in logs)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"messageId": None},
        {"messageId": 3},
        {"messageId": " "},
        {"messageId": ""},
        record(),
    ],
)
async def test_invalid_later_record_prevents_all_processing(consumer, bad):
    with capture_logs() as logs, pytest.raises(EmbeddingSqsInputError) as caught:
        await process_embedding_messages(
            {"Records": [record(), bad]}, consumer=consumer
        )
    consumer.consume.assert_not_awaited()
    assert caught.value.record_index == 1
    assert len(logs) == 1
    assert "message_id" not in logs[0]
    assert logs[0]["record_index"] == 1


@pytest.mark.parametrize(
    "event",
    [None, [], {}, {"Records": None}, {"Records": {}}, {"Records": "private-input"}],
)
async def test_invalid_delivery_shape_is_safe(consumer, event):
    with capture_logs() as logs, pytest.raises(EmbeddingSqsInputError) as caught:
        await process_embedding_messages(event, consumer=consumer)
    assert "private-input" not in str(caught.value)
    assert "private-input" not in repr(logs)
    consumer.consume.assert_not_awaited()


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"body": None},
        {"body": 3},
        {"body": "private-json"},
        {"body": '{"private-field":"private-value"}'},
    ],
)
async def test_invalid_body_fails_only_its_message(consumer, bad):
    with capture_logs() as logs:
        response = await process_embedding_messages(
            {"Records": [{"messageId": "bad", **bad}, record("good")]},
            consumer=consumer,
        )
    assert response == {"batchItemFailures": [{"itemIdentifier": "bad"}]}
    consumer.consume.assert_awaited_once()
    assert logs[0]["event"] == "embedding_message_input_invalid"
    assert "private-" not in repr(logs)
    assert "event_id" not in logs[0]


@pytest.mark.parametrize("log_failure", [False, True])
async def test_mixed_results_continue_sequentially_and_ignore_log_failure(
    consumer, log_failure, monkeypatch
):
    seen = []
    active = False

    async def consume(payload):
        nonlocal active
        assert not active
        active = True
        await asyncio.sleep(0)
        active = False
        seen.append(payload.analyzed_article_id)
        if payload.analyzed_article_id in (1, 3):
            raise RuntimeError("private-exception")
        return EmbeddingCompletion(EmbeddingCompletionReason.ALREADY_EMBEDDED)

    consumer.consume.side_effect = consume
    if log_failure:
        monkeypatch.setattr(
            module.logger, "info", Mock(side_effect=RuntimeError("private-log"))
        )
        monkeypatch.setattr(
            module.logger, "warning", Mock(side_effect=RuntimeError("private-log"))
        )
    with capture_logs() as logs:
        response = await process_embedding_messages(
            {"Records": [record(f"msg-{i}", i) for i in (1, 2, 3)]}, consumer=consumer
        )
    assert seen == [1, 2, 3]
    assert response == {
        "batchItemFailures": [{"itemIdentifier": "msg-1"}, {"itemIdentifier": "msg-3"}]
    }
    assert "private-" not in repr(logs)
    if not log_failure:
        assert logs[0]["error_class"] == "builtins.RuntimeError"
        assert logs[0]["event_id"] == str(UUID(int=1))
        assert logs[1]["reason"] == "already_embedded"


@pytest.mark.parametrize(
    "completion",
    [None, True, "saved", EmbeddingCompletion("saved"), EmbeddingCompletion("future")],
)
async def test_contract_violation_is_not_success(consumer, completion):
    consumer.consume.return_value = completion
    assert await process_embedding_messages(
        {"Records": [record()]}, consumer=consumer
    ) == {"batchItemFailures": [{"itemIdentifier": "msg-1"}]}


async def test_all_failed_ids_are_preserved_without_normalizing(consumer):
    consumer.consume.side_effect = TimeoutError()
    ids = [" msg-1 ", "msg-2"]
    assert await process_embedding_messages(
        {"Records": [record(i) for i in ids]}, consumer=consumer
    ) == {"batchItemFailures": [{"itemIdentifier": i} for i in ids]}


@pytest.mark.parametrize(
    "exc", [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit()]
)
async def test_cancellation_and_process_exit_propagate(consumer, exc):
    consumer.consume.side_effect = exc
    with pytest.raises(type(exc)) as caught:
        await process_embedding_messages(
            {"Records": [record(), record("msg-2", 2)]}, consumer=consumer
        )
    assert caught.value is exc
    consumer.consume.assert_awaited_once()


async def test_input_logging_failure_preserves_original_failure(consumer, monkeypatch):
    monkeypatch.setattr(
        module.logger, "warning", Mock(side_effect=RuntimeError("private-log"))
    )
    with pytest.raises(EmbeddingSqsInputError):
        await process_embedding_messages({"Records": [record(), {}]}, consumer=consumer)
    consumer.consume.assert_not_awaited()
    assert await process_embedding_messages(
        {"Records": [{"messageId": "bad"}, record()]}, consumer=consumer
    ) == {"batchItemFailures": [{"itemIdentifier": "bad"}]}
    consumer.consume.assert_awaited_once()
