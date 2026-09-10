"""配送構造の事前確認と、メッセージ単位の応答・ログ境界を検証する。"""

import asyncio
import json
from importlib import import_module
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from structlog.testing import capture_logs

from app.analysis.assessment.events import ArticleAssessedInScope
from app.analysis.embedding.consumer import EmbeddingConsumer
from app.analysis.embedding.service import (
    EmbeddingCompletion,
)
from app.lambda_handlers.sqs.errors import SqsInputError, SqsInputReason
from tests.lambda_handlers.embedding_fixtures import run_embedding as run_embedding

module = import_module("app.lambda_handlers.embedding.handler")

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
    result.consume = AsyncMock(return_value=EmbeddingCompletion.SAVED)
    return result


@pytest.mark.parametrize(
    "records", [[], [record()], [record("msg-1", 1), record("msg-2", 2)]]
)
async def test_success_returns_no_failed_items_and_preserves_input_order(
    run_embedding, consumer, records
):
    with capture_logs() as logs:
        failed_items = await run_embedding({"Records": records, "unused": "ignored"})
    assert failed_items == []
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
async def test_invalid_later_record_prevents_all_processing(
    run_embedding, consumer, bad
):
    with capture_logs() as logs, pytest.raises(SqsInputError) as caught:
        await run_embedding({"Records": [record(), bad]})
    consumer.consume.assert_not_awaited()
    assert caught.value.record_index == 1
    assert len(logs) == 1
    assert "message_id" not in logs[0]
    assert logs[0]["record_index"] == 1


@pytest.mark.parametrize(
    "event",
    [None, [], {}, {"Records": None}, {"Records": {}}, {"Records": "private-input"}],
)
async def test_invalid_delivery_shape_is_safe(run_embedding, consumer, event):
    with capture_logs() as logs, pytest.raises(SqsInputError) as caught:
        await run_embedding(event)
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
async def test_invalid_body_fails_only_its_message(run_embedding, consumer, bad):
    with capture_logs() as logs:
        failed_items = await run_embedding(
            {"Records": [{"messageId": "bad", **bad}, record("good")]},
        )
    assert failed_items == [{"itemIdentifier": "bad"}]
    consumer.consume.assert_awaited_once()
    assert logs[0]["event"] == "embedding_message_input_invalid"
    assert "private-" not in repr(logs)
    assert "event_id" not in logs[0]


@pytest.mark.parametrize("log_failure", [False, True])
async def test_mixed_results_continue_sequentially_and_ignore_log_failure(
    run_embedding, consumer, log_failure, monkeypatch
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
        return EmbeddingCompletion.ALREADY_EMBEDDED

    consumer.consume.side_effect = consume
    if log_failure:
        monkeypatch.setattr(
            module.logger, "info", Mock(side_effect=RuntimeError("private-log"))
        )
        monkeypatch.setattr(
            module.logger,
            "warning",
            Mock(side_effect=RuntimeError("private-log")),
        )
    with capture_logs() as logs:
        failed_items = await run_embedding(
            {"Records": [record(f"msg-{i}", i) for i in (1, 2, 3)]}
        )
    assert seen == [1, 2, 3]
    assert failed_items == [{"itemIdentifier": "msg-1"}, {"itemIdentifier": "msg-3"}]
    assert "private-" not in repr(logs)
    if not log_failure:
        assert logs[0]["error_class"] == "builtins.RuntimeError"
        assert logs[0]["event_id"] == str(UUID(int=1))
        assert logs[1]["reason"] == "already_embedded"


@pytest.mark.parametrize(
    ("completion", "reason"),
    [
        (EmbeddingCompletion.SAVED, "saved"),
        (EmbeddingCompletion.ALREADY_EMBEDDED, "already_embedded"),
    ],
)
async def test_normal_completion_is_logged_without_batch_failure(
    run_embedding, consumer, completion, reason
):
    """保存完了と生成済みを成功として応答し、完了理由を記録する。"""
    consumer.consume.return_value = completion
    with capture_logs() as logs:
        failed_items = await run_embedding({"Records": [record()]})
    assert failed_items == []
    consumer.consume.assert_awaited_once_with(
        ArticleAssessedInScope(curation_id=1, analyzed_article_id=1)
    )
    assert len(logs) == 1
    assert logs[0]["event"] == "embedding_message_completed"
    assert logs[0]["message_id"] == "msg-1"
    assert logs[0]["reason"] == reason


async def test_all_failed_ids_are_preserved_without_normalizing(
    run_embedding, consumer
):
    consumer.consume.side_effect = TimeoutError()
    ids = [" msg-1 ", "msg-2"]
    assert await run_embedding({"Records": [record(i) for i in ids]}) == [
        {"itemIdentifier": message_id} for message_id in ids
    ]


@pytest.mark.parametrize(
    "exc", [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit()]
)
async def test_cancellation_and_process_exit_propagate(run_embedding, consumer, exc):
    consumer.consume.side_effect = exc
    with pytest.raises(type(exc)) as caught:
        await run_embedding({"Records": [record(), record("msg-2", 2)]})
    assert caught.value is exc
    consumer.consume.assert_awaited_once()


async def test_input_logging_failure_preserves_original_failure(
    run_embedding, consumer, monkeypatch
):
    monkeypatch.setattr(
        module.logger, "warning", Mock(side_effect=RuntimeError("private-log"))
    )
    with pytest.raises(SqsInputError):
        await run_embedding({"Records": [record(), {}]})
    consumer.consume.assert_not_awaited()
    assert await run_embedding({"Records": [{"messageId": "bad"}, record()]}) == [
        {"itemIdentifier": "bad"}
    ]
    consumer.consume.assert_awaited_once()


@pytest.mark.parametrize(
    ("body_fields", "code"),
    [
        ({}, "missing_required_field"),
        ({"body": None}, "invalid_type"),
        ({"body": 3}, "invalid_type"),
    ],
)
async def test_body_shape_failure_preserves_diagnostic_and_exact_message_id(
    run_embedding, consumer, body_fields, code
):
    """本文不正を個別失敗として扱い、欠落と型不正の診断を維持する。"""
    with capture_logs() as logs:
        failed_items = await run_embedding(
            {"Records": [{"messageId": " bad ", **body_fields}, record("good")]},
        )
    assert failed_items == [{"itemIdentifier": " bad "}]
    assert logs[0]["reason"] == "invalid_body"
    assert logs[0]["issues"] == [{"field": "body", "code": code}]
    consumer.consume.assert_awaited_once()


async def test_duplicate_id_precedes_body_validation(run_embedding, consumer):
    """本文不正が先にあっても、一覧のID不正を全体失敗として先に確定する。"""
    with capture_logs() as logs, pytest.raises(SqsInputError) as caught:
        await run_embedding(
            {"Records": [{"messageId": "duplicate"}, record("duplicate")]},
        )
    assert caught.value.reason is SqsInputReason.DUPLICATE_MESSAGE_ID
    assert caught.value.record_index == 1
    assert [log["event"] for log in logs] == ["embedding_sqs_input_invalid"]
    consumer.consume.assert_not_awaited()


async def test_invalid_event_after_success_does_not_reuse_previous_event(
    run_embedding, consumer
):
    """直前が成功しても不正本文の処理・診断に前のイベントを流用しない。"""
    with capture_logs() as logs:
        failed_items = await run_embedding(
            {
                "Records": [
                    record("first", 1),
                    {"messageId": "invalid", "body": "private-invalid-json"},
                    record("last", 3),
                ]
            }
        )
    assert failed_items == [{"itemIdentifier": "invalid"}]
    assert [
        call.args[0].analyzed_article_id for call in consumer.consume.await_args_list
    ] == [
        1,
        3,
    ]
    assert logs[1]["event"] == "embedding_message_input_invalid"
    assert logs[1]["message_id"] == "invalid"
    assert "event_id" not in logs[1]
    assert "analyzed_article_id" not in logs[1]
    assert "private-invalid-json" not in repr(logs)
