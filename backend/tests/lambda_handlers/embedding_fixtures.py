"""資源の初期化を差し替えて実際のLambda処理へConsumerを接続する。"""

from contextlib import asynccontextmanager
from functools import partial
from importlib import import_module

import pytest

from app.lambda_handlers.embedding.settings import EmbeddingConsumerSettings


@pytest.fixture
def run_embedding(consumer, monkeypatch):
    module = import_module("app.lambda_handlers.embedding.handler")
    settings = EmbeddingConsumerSettings(
        env="test",
        aws_region="ap-northeast-1",
        database_url="postgresql+asyncpg://test@db.invalid/vector",
        db_iam_auth=True,
        gemini_api_key_parameter_path="/test/gemini-key",
    )

    @asynccontextmanager
    async def open_consumer(_settings):
        yield consumer

    monkeypatch.setattr(module, "open_embedding_consumer", open_consumer)
    return partial(module._run_embedding, settings=settings)
