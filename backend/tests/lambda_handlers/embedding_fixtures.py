"""資源の初期化を差し替えて実際のLambda処理へConsumerを接続する。"""

from contextlib import asynccontextmanager
from functools import partial
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from app.lambda_handlers.embedding.settings import EmbeddingConsumerSettings


@pytest.fixture
def run_embedding(consumer, monkeypatch):
    module = import_module("app.lambda_handlers.embedding.handler")
    composition = import_module("app.lambda_handlers.embedding.composition")
    settings = EmbeddingConsumerSettings(
        env="test",
        aws_region="ap-northeast-1",
        database_url="postgresql+asyncpg://test@db.invalid/vector",
        db_iam_auth=True,
        gemini_api_key_parameter_path="/test/gemini-key",
    )

    @asynccontextmanager
    async def open_resources(_settings):
        yield SimpleNamespace(
            session_factory=Mock(), gemini_api_key=SecretStr("test-key")
        )

    @asynccontextmanager
    async def open_client(**_kwargs):
        yield Mock()

    monkeypatch.setattr(composition, "open_embedding_resources", open_resources)
    monkeypatch.setattr(composition, "open_gemini_client", open_client)
    monkeypatch.setattr(composition, "EmbeddingConsumer", lambda *_args: consumer)
    return partial(module._run_embedding, settings=settings)
