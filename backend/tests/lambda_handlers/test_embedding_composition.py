"""EmbeddingConsumerの配線と、初期化・利用・終了の境界を確認する。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from app.ai_providers.gemini.settings import GeminiConnectionSettings
from app.analysis.embedding.consumer import EmbeddingConsumer
from app.analysis.embedding.embedder import GeminiEmbedder
from app.lambda_handlers.embedding import composition as module

pytestmark = pytest.mark.unit


@pytest.fixture
def wiring(monkeypatch):
    state = SimpleNamespace(
        order=[],
        settings=object(),
        resources=SimpleNamespace(
            session_factory=Mock(), gemini_api_key=SecretStr("test-key")
        ),
        client=Mock(),
        log=Mock(),
    )

    @asynccontextmanager
    async def open_resources(settings):
        state.order.append("resources_open")
        try:
            yield state.resources
        finally:
            state.order.append("resources_close")

    @asynccontextmanager
    async def open_client(**kwargs):
        state.order.append("client_open")
        try:
            yield state.client
        finally:
            state.order.append("client_close")

    state.open_resources = Mock(side_effect=open_resources)
    state.open_client = Mock(side_effect=open_client)
    state.constructor = Mock(wraps=EmbeddingConsumer)
    monkeypatch.setattr(module, "open_embedding_resources", state.open_resources)
    monkeypatch.setattr(module, "open_gemini_client", state.open_client)
    monkeypatch.setattr(module, "EmbeddingConsumer", state.constructor)
    monkeypatch.setattr(module, "logger", state.log)
    return state


@pytest.mark.asyncio
async def test_wires_borrowed_client_and_session_factory(wiring):
    """実ConsumerとEmbedderへ準備済みのDB資源とGeminiクライアントを渡す。"""
    async with module.open_embedding_consumer(wiring.settings) as consumer:
        assert isinstance(consumer, EmbeddingConsumer)
        assert consumer._session_factory is wiring.resources.session_factory
        assert isinstance(consumer._embedder, GeminiEmbedder)
        assert consumer._embedder._client is wiring.client

    wiring.open_resources.assert_called_once_with(wiring.settings)
    wiring.open_client.assert_called_once_with(
        api_key=wiring.resources.gemini_api_key, settings=GeminiConnectionSettings()
    )


@pytest.mark.asyncio
async def test_normal_exit_closes_resources_in_reverse_order(wiring):
    """利用中は資源を維持し、利用終了後に取得と逆の順序で閉じる。"""
    async with module.open_embedding_consumer(wiring.settings):
        assert wiring.order == ["resources_open", "client_open"]

    assert wiring.order == [
        "resources_open",
        "client_open",
        "client_close",
        "resources_close",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dependency,stage,expected_order",
    [
        ("open_resources", "resources", []),
        ("open_client", "gemini_client", ["resources_open", "resources_close"]),
        (
            "constructor",
            "consumer",
            ["resources_open", "client_open", "client_close", "resources_close"],
        ),
    ],
)
async def test_initialization_failure_closes_acquired_resources_and_propagates(
    wiring, dependency, stage, expected_order
):
    """初期化失敗は段階を安全に記録し、取得済み資源を閉じて元例外を伝える。"""
    original = RuntimeError("private-initialization")
    getattr(wiring, dependency).side_effect = original

    with pytest.raises(RuntimeError) as caught:
        async with module.open_embedding_consumer(wiring.settings):
            pytest.fail("初期化失敗時にConsumerを貸し出してはいけない")

    assert caught.value is original
    assert wiring.order == expected_order
    wiring.log.warning.assert_called_once_with(
        "embedding_initialization_failed",
        stage=stage,
        error_class="builtins.RuntimeError",
    )


@pytest.mark.asyncio
async def test_initialization_log_failure_preserves_original_exception(wiring):
    """初期化失敗の診断が壊れても、元の例外を置き換えない。"""
    original = RuntimeError("consumer-failed")
    wiring.constructor.side_effect = original
    wiring.log.warning.side_effect = RuntimeError("log-failed")

    with pytest.raises(RuntimeError) as caught:
        async with module.open_embedding_consumer(wiring.settings):
            pytest.fail("初期化失敗時にConsumerを貸し出してはいけない")

    assert caught.value is original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        ValueError("business"),
        asyncio.CancelledError(),
        KeyboardInterrupt(),
        SystemExit(),
    ],
)
async def test_borrower_failure_is_not_initialization_failure(wiring, failure):
    """利用側の例外は初期化失敗とせず、資源を閉じて伝播する。"""
    with pytest.raises(type(failure)) as caught:
        async with module.open_embedding_consumer(wiring.settings):
            raise failure

    assert caught.value is failure
    wiring.log.warning.assert_not_called()
    assert wiring.order[-2:] == ["client_close", "resources_close"]


@pytest.mark.asyncio
async def test_initialization_cancellation_is_not_a_business_failure(wiring):
    """初期化中のキャンセルは失敗診断へ変換せず、取得済み資源を閉じる。"""
    original = asyncio.CancelledError()
    wiring.open_client.side_effect = original

    with pytest.raises(asyncio.CancelledError) as caught:
        async with module.open_embedding_consumer(wiring.settings):
            pytest.fail("キャンセル時にConsumerを貸し出してはいけない")

    assert caught.value is original
    wiring.log.warning.assert_not_called()
    assert wiring.order == ["resources_open", "resources_close"]
