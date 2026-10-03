# ruff: noqa: S101
"""実Curationの接続設定と外部通信境界の差し替え、DB上の待機の制御を共有する。"""

import asyncio
from dataclasses import dataclass, field
from queue import Empty, Queue
from threading import Event
from unittest.mock import AsyncMock, Mock

import httpx2
import pytest
from pydantic import SecretStr
from sqlalchemy import text

from app.ai_providers.gemini import client as gemini_module
from app.analysis.curation.repository import CurationRepository
from app.lambda_handlers import article_analysis_lifecycle as resource_module
from app.lambda_handlers.curation.settings import CurationConsumerSettings
from local_tests.curation.support import handler_module
from tests.iam_fixtures import inject_test_db_signer


@pytest.fixture
def gemini_response():
    return AsyncMock()


@pytest.fixture
def curation_runtime(system_database, monkeypatch, gemini_response):
    settings = CurationConsumerSettings(
        env="test",
        aws_region="ap-northeast-1",
        database_url=inject_test_db_signer(
            monkeypatch,
            system_database.url("vector_article_analysis", sqlalchemy=True),
            resources_module=resource_module,
        ),
        db_iam_auth=True,
        gemini_api_key_parameter_path="/test/gemini-key",
    )
    monkeypatch.setattr(handler_module, "CurationConsumerSettings", lambda: settings)
    monkeypatch.setattr(
        resource_module,
        "get_secret_parameter",
        Mock(return_value=SecretStr("test-key")),
    )

    def http_factory(**kwargs):
        kwargs.pop("retries")
        return httpx2.AsyncClient(  # noqa: TID251
            transport=httpx2.MockTransport(gemini_response), **kwargs
        )

    monkeypatch.setattr(gemini_module, "make_external_async_client", http_factory)


async def wait_for_signal(signal, message):
    if not await asyncio.to_thread(signal.wait, 10):
        raise TimeoutError(message)


@dataclass
class AiResponseGate:
    response: httpx2.Response
    requested: Event = field(default_factory=Event)
    allow_response: Event = field(default_factory=Event)

    async def wait_requested(self):
        await wait_for_signal(self.requested, "AIリクエストが到達しなかった")

    def release(self):
        self.allow_response.set()


@pytest.fixture
def gated_ai_responses(gemini_response):
    pending = Queue()
    registered = []

    async def respond(request):
        try:
            gate = pending.get_nowait()
        except Empty:
            pytest.fail("登録数を超えてAIが呼ばれた")
        gate.requested.set()
        await wait_for_signal(gate.allow_response, "AI応答の再開指示が届かなかった")
        return gate.response

    gemini_response.side_effect = respond

    def register(response):
        gate = AiResponseGate(response)
        registered.append(gate)
        pending.put(gate)
        return gate

    yield register
    for gate in registered:
        gate.release()


@dataclass
class CommitHold:
    inserted_pids: list[int] = field(default_factory=list)
    insert_finished: Event = field(default_factory=Event)
    allow_commit: Event = field(default_factory=Event)

    async def wait_inserted(self):
        await wait_for_signal(self.insert_finished, "先行側のINSERTが完了しなかった")
        assert len(self.inserted_pids) == 1
        return self.inserted_pids[0]

    def release(self):
        self.allow_commit.set()


@pytest.fixture
def commit_hold(monkeypatch):
    """結果の実INSERT後だけ停止し、重複スキップの処理には手を加えない。"""
    hold = CommitHold()

    def wrap(save):
        async def pause_after_insert(repository, *args, **kwargs):
            saved = await save(repository, *args, **kwargs)
            if saved is not None:
                pid = await repository._session.scalar(text("SELECT pg_backend_pid()"))
                hold.inserted_pids.append(pid)
                hold.insert_finished.set()
                await wait_for_signal(
                    hold.allow_commit, "保存確定の再開指示が届かなかった"
                )
            return saved

        return pause_after_insert

    for name in ("save_signal", "save_noise"):
        monkeypatch.setattr(
            CurationRepository, name, wrap(getattr(CurationRepository, name))
        )
    yield hold
    hold.release()
