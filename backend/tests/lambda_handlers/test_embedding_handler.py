"""Lambda入口の資源共有・初期化失敗・終了後の応答を検証する。"""

import asyncio
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from app.ai_providers.gemini import client as gemini_module
from app.ai_providers.gemini.settings import GeminiConnectionSettings
from app.analysis.embedding.embedder import GeminiEmbedder
from app.analysis.embedding.service import (
    EmbeddingCompletion,
)
from app.lambda_handlers.embedding import resources as resource_module
from app.lambda_handlers.embedding.settings import EmbeddingConsumerSettings
from app.lambda_handlers.sqs.errors import SqsInputError
from tests.lambda_handlers.test_embedding import record

module = import_module("app.lambda_handlers.embedding.handler")

pytestmark = pytest.mark.unit


@pytest.fixture
def wiring(monkeypatch):
    config = EmbeddingConsumerSettings(
        env="test",
        aws_region="ap-northeast-1",
        database_url="postgresql+asyncpg://test:test@db.invalid/vector",
        db_iam_auth=False,
        gemini_api_key_parameter_path="/test/gemini-key",
    )
    steps = []
    engines, sdks, http_clients, consumers = [], [], [], []
    secret = Mock(side_effect=lambda **_: SecretStr(f"private-key-{len(engines)}"))
    monkeypatch.setattr(resource_module, "get_secret_parameter", secret)

    def create_engine(*args, **kwargs):
        steps.append("engine_open")
        engine = SimpleNamespace(
            dispose=AsyncMock(side_effect=lambda: steps.append("engine_close"))
        )
        engines.append(engine)
        return engine

    engine_factory = Mock(side_effect=create_engine)
    monkeypatch.setattr(
        resource_module, "create_embedding_consumer_engine", engine_factory
    )
    monkeypatch.setattr(
        resource_module, "caller_managed_session_factory", lambda _: Mock()
    )

    def create_http(**kwargs):
        steps.append("http_open")
        http = Mock(
            spec=httpx.AsyncClient,
            aclose=AsyncMock(side_effect=lambda: steps.append("http_close")),
        )
        http_clients.append(http)
        return http

    monkeypatch.setattr(gemini_module, "make_external_async_client", create_http)

    def create_sdk(**kwargs):
        steps.append("sdk_open")
        sdk = SimpleNamespace(
            aio=SimpleNamespace(
                aclose=AsyncMock(side_effect=lambda: steps.append("aio_close"))
            ),
            close=Mock(side_effect=lambda: steps.append("sdk_close")),
        )
        sdks.append(sdk)
        return sdk

    sdk_factory = Mock(side_effect=create_sdk)
    monkeypatch.setattr(gemini_module.genai, "Client", sdk_factory)

    def create_consumer(factory, embedder):
        assert isinstance(embedder, GeminiEmbedder)
        assert embedder._client is sdks[-1].aio
        consumer = SimpleNamespace(
            consume=AsyncMock(return_value=EmbeddingCompletion.SAVED),
            session_factory=factory,
        )
        consumers.append(consumer)
        return consumer

    constructor = Mock(side_effect=create_consumer)
    monkeypatch.setattr(module, "EmbeddingConsumer", constructor)
    settings_factory = Mock(return_value=config)
    monkeypatch.setattr(module, "EmbeddingConsumerSettings", settings_factory)
    return SimpleNamespace(
        config=config,
        steps=steps,
        engines=engines,
        sdks=sdks,
        http_clients=http_clients,
        consumers=consumers,
        secret=secret,
        engine_factory=engine_factory,
        sdk_factory=sdk_factory,
        constructor=constructor,
        settings_factory=settings_factory,
    )


def test_handler_recreates_resources_and_closes_before_return(wiring):
    """同期入口の連続呼び出しが別の資源を所有する。"""
    for i in range(2):
        assert module.handler({"Records": [record(), record("second")]}, object()) == {
            "batchItemFailures": []
        }
        assert wiring.steps[i * 7 :] == [
            "engine_open",
            "http_open",
            "sdk_open",
            "aio_close",
            "sdk_close",
            "http_close",
            "engine_close",
        ]
        assert wiring.consumers[i].consume.await_count == 2
    assert wiring.secret.call_count == wiring.settings_factory.call_count == 2
    assert wiring.engines[0] is not wiring.engines[1]
    assert wiring.sdks[0].aio is not wiring.sdks[1].aio
    assert (
        wiring.consumers[0].session_factory is not wiring.consumers[1].session_factory
    )
    assert [c.kwargs["api_key"] for c in wiring.sdk_factory.call_args_list] == [
        "private-key-0",
        "private-key-1",
    ]


@pytest.mark.parametrize(
    ("failed_article_ids", "expected_ids"),
    [
        ([], []),
        ([1, 3], [" msg-1 ", "msg-3"]),
        ([1, 2, 3], [" msg-1 ", "msg-2", "msg-3"]),
    ],
)
def test_handler_reports_only_failed_ids_in_aws_format(
    wiring, failed_article_ids, expected_ids
):
    """公開入口が失敗IDの順序と原文を保ち、資源終了後にAWS応答を返す。"""
    original = wiring.constructor.side_effect

    def create(*args):
        consumer = original(*args)

        async def consume(payload):
            if payload.analyzed_article_id in failed_article_ids:
                raise RuntimeError("processing_failed")
            return EmbeddingCompletion.SAVED

        consumer.consume.side_effect = consume
        return consumer

    wiring.constructor.side_effect = create
    response = module.handler(
        {"Records": [record(" msg-1 ", 1), record("msg-2", 2), record("msg-3", 3)]},
        None,
    )
    assert response == {
        "batchItemFailures": [
            {"itemIdentifier": message_id} for message_id in expected_ids
        ]
    }
    assert wiring.consumers[0].consume.await_count == 3
    assert wiring.steps[-4:] == ["aio_close", "sdk_close", "http_close", "engine_close"]


@pytest.mark.parametrize(
    "event", [{"Records": []}, {}, {"Records": [record(), record()]}]
)
def test_empty_or_invalid_input_is_handled_after_initialization(wiring, event):
    """空入力も配送構造不正も初期化を先に完了する。"""
    with capture_logs() as logs:
        if event == {"Records": []}:
            assert module.handler(event, None) == {"batchItemFailures": []}
        else:
            with pytest.raises(SqsInputError):
                module.handler(event, None)
    wiring.consumers[0].consume.assert_not_awaited()
    assert wiring.steps[-4:] == ["aio_close", "sdk_close", "http_close", "engine_close"]
    assert not any(log["event"] == "embedding_initialization_failed" for log in logs)


@pytest.mark.parametrize(
    "stage", ["settings", "resources", "gemini_client", "consumer"]
)
@pytest.mark.parametrize("log_fails", [False, True])
def test_initialization_failure_propagates_and_closes_created_resources(
    wiring, monkeypatch, stage, log_fails
):
    """初期化障害は入力が空でも失敗とし、ログ障害で元の例外を変えない。"""
    failure = RuntimeError("private-key private-url private-event")
    {
        "settings": wiring.settings_factory,
        "resources": wiring.secret,
        "gemini_client": wiring.sdk_factory,
        "consumer": wiring.constructor,
    }[stage].side_effect = failure
    log = Mock(side_effect=RuntimeError("private-log") if log_fails else None)
    monkeypatch.setattr(module.logger, "warning", log)
    with pytest.raises(RuntimeError) as caught:
        module.handler({"Records": []}, None)
    assert caught.value is failure
    log.assert_called_once_with(
        "embedding_initialization_failed",
        stage=stage,
        error_class="builtins.RuntimeError",
    )
    assert "private-" not in repr(log.call_args_list)
    if stage in ("settings", "resources"):
        assert not wiring.engines
    else:
        wiring.engines[0].dispose.assert_awaited_once()
        wiring.http_clients[0].aclose.assert_awaited_once()
    if stage == "consumer":
        wiring.sdks[0].aio.aclose.assert_awaited_once()
        wiring.sdks[0].close.assert_called_once()
    assert not wiring.consumers


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_at", ["resources", "consumer", "processing"])
async def test_cancellation_propagates_without_initialization_failure_log(
    wiring, cancel_at
):
    """キャンセルを通常の失敗へ変換せず、開いた資源を閉じる。"""
    cancellation = asyncio.CancelledError()
    if cancel_at == "resources":
        wiring.secret.side_effect = cancellation
    elif cancel_at == "consumer":
        wiring.constructor.side_effect = cancellation
    else:
        original = wiring.constructor.side_effect

        def create(*args):
            consumer = original(*args)
            consumer.consume.side_effect = cancellation
            return consumer

        wiring.constructor.side_effect = create
    with capture_logs() as logs, pytest.raises(asyncio.CancelledError) as caught:
        await module._run_embedding({"Records": [record()]}, wiring.config)
    assert caught.value is cancellation
    assert logs == []
    if wiring.engines:
        assert wiring.steps[-4:] == [
            "aio_close",
            "sdk_close",
            "http_close",
            "engine_close",
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("processing_failure", [False, True])
async def test_cleanup_and_log_failures_preserve_processing_result(
    wiring, monkeypatch, processing_failure
):
    """実資源管理の終了障害を通して応答と先行例外を維持する。"""
    original = wiring.constructor.side_effect
    failure = RuntimeError("private-processing")

    def create(*args):
        consumer = original(*args)
        for close in (
            wiring.engines[-1].dispose,
            wiring.http_clients[-1].aclose,
            wiring.sdks[-1].aio.aclose,
            wiring.sdks[-1].close,
        ):
            close.side_effect = RuntimeError("private-cleanup")
        return consumer

    wiring.constructor.side_effect = create
    for logger in (module.logger, resource_module.logger, gemini_module.logger):
        monkeypatch.setattr(
            logger, "warning", Mock(side_effect=RuntimeError("private-log"))
        )
    if processing_failure:
        monkeypatch.setattr(
            module.SqsRecordBatch, "from_lambda_event", Mock(side_effect=failure)
        )
        with pytest.raises(RuntimeError) as caught:
            await module._run_embedding({"Records": [record()]}, wiring.config)
        assert caught.value is failure
    else:
        assert await module._run_embedding({"Records": []}, wiring.config) == []
    wiring.engines[0].dispose.assert_awaited_once()
    wiring.http_clients[0].aclose.assert_awaited_once()
    wiring.sdks[0].aio.aclose.assert_awaited_once()
    wiring.sdks[0].close.assert_called_once()


@pytest.mark.asyncio
async def test_mixed_messages_share_consumer_and_continue_in_order(wiring, monkeypatch):
    """個別失敗の後も同じConsumerで入力順に処理する。"""
    original = wiring.constructor.side_effect
    seen = []

    def create(*args):
        consumer = original(*args)

        async def consume(payload):
            seen.append(payload.analyzed_article_id)
            await asyncio.sleep(0)
            if payload.analyzed_article_id == 2:
                raise RuntimeError("private")
            return (
                EmbeddingCompletion.SAVED
                if payload.analyzed_article_id == 1
                else EmbeddingCompletion.ALREADY_EMBEDDED
            )

        consumer.consume.side_effect = consume
        return consumer

    wiring.constructor.side_effect = create
    gemini_open = Mock(wraps=module.open_gemini_client)
    monkeypatch.setattr(module, "open_gemini_client", gemini_open)
    with capture_logs() as logs:
        failed_items = await module._run_embedding(
            {"Records": [record(str(i), i) for i in (1, 2, 3)]}, wiring.config
        )
    assert failed_items == [{"itemIdentifier": "2"}]
    assert seen == [1, 2, 3]
    wiring.constructor.assert_called_once()
    assert gemini_open.call_args.kwargs["settings"] == GeminiConnectionSettings()
    assert [log["event"] for log in logs] == [
        "embedding_message_completed",
        "embedding_message_failed",
        "embedding_message_completed",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [asyncio.CancelledError(), SystemExit(1)])
async def test_shutdown_control_exceptions_propagate_and_close_remaining_resources(
    wiring, failure
):
    """終了中のキャンセル・プロセス終了も伝播し、残りの資源終了を試みる。"""
    original = wiring.constructor.side_effect

    def create(*args):
        consumer = original(*args)
        wiring.sdks[-1].aio.aclose.side_effect = failure
        return consumer

    wiring.constructor.side_effect = create
    with capture_logs() as logs, pytest.raises(type(failure)) as caught:
        await module._run_embedding({"Records": []}, wiring.config)
    assert caught.value is failure
    assert logs == []
    wiring.sdks[0].close.assert_called_once()
    wiring.http_clients[0].aclose.assert_awaited_once()
    wiring.engines[0].dispose.assert_awaited_once()
