"""Embeddingの実処理で共有する接続設定と外部通信境界の差し替え。"""

import asyncio
from dataclasses import dataclass, field
from importlib import import_module
from queue import Empty, Queue
from threading import Event
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.ai_providers.gemini import client as gemini_module
from app.analysis.embedding.consumer import EmbeddingConsumer
from app.analysis.embedding.domain.value_objects import EMBEDDING_DIMENSION
from app.analysis.embedding.repository import EmbeddingRepository
from app.analysis.embedding.service import EmbeddingCompletion
from app.lambda_handlers import article_analysis_lifecycle as resource_module
from app.lambda_handlers.embedding.settings import EmbeddingConsumerSettings
from app.models.analyzed_article_record import AnalyzedArticleRecord
from tests.iam_fixtures import inject_test_db_signer

handler_module = import_module("app.lambda_handlers.embedding.handler")
HANDOFF_TIMEOUT = 10


@dataclass
class SaveFailureObservation:
    updated_before_error: list[tuple[int, bool, bool | None]] = field(
        default_factory=list
    )
    database_errors: list[DBAPIError] = field(default_factory=list)


@pytest.fixture
def database_error_after_update(monkeypatch):
    """実UPDATEの結果を同じトランザクションで確認し、コミット前にDBエラーを起こす。"""
    observation = SaveFailureObservation()
    original_save = EmbeddingRepository.save

    async def save_then_fail(self, vector, *, analyzed_article_id):
        saved = await original_save(
            self, vector, analyzed_article_id=analyzed_article_id
        )
        has_embedding = await self._session.scalar(
            select(AnalyzedArticleRecord.embedding.is_not(None)).where(
                AnalyzedArticleRecord.id == analyzed_article_id
            )
        )
        observation.updated_before_error.append(
            (analyzed_article_id, saved, has_embedding)
        )
        try:
            await self._session.execute(text("SELECT 1 / 0"))
        except DBAPIError as exc:
            observation.database_errors.append(exc)
            raise
        return saved

    monkeypatch.setattr(EmbeddingRepository, "save", save_then_fail)
    return observation


@pytest.fixture
def gemini_response():
    return AsyncMock(
        return_value=httpx.Response(
            200, json={"embeddings": [{"values": [0.2] * EMBEDDING_DIMENSION}]}
        )
    )


@pytest.fixture
def embedding_runtime(system_database, monkeypatch, gemini_response):
    settings = EmbeddingConsumerSettings(
        env="test",
        aws_region="ap-northeast-1",
        database_url=inject_test_db_signer(
            monkeypatch,
            system_database.url("vector_app", sqlalchemy=True),
            resources_module=resource_module,
        ),
        db_iam_auth=True,
        gemini_api_key_parameter_path="/test/gemini-key",
    )
    monkeypatch.setattr(handler_module, "EmbeddingConsumerSettings", lambda: settings)
    monkeypatch.setattr(
        resource_module,
        "get_secret_parameter",
        Mock(return_value=SecretStr("test-private-key")),
    )

    def http_factory(**kwargs):
        kwargs.pop("retries")
        return httpx.AsyncClient(  # noqa: TID251
            transport=httpx.MockTransport(gemini_response), **kwargs
        )

    monkeypatch.setattr(gemini_module, "make_external_async_client", http_factory)


async def _wait_for_signal(signal: Event, message: str) -> None:
    # handlerは別スレッドの別イベントループで動くため、threading.Eventで同期する。
    if not await asyncio.to_thread(signal.wait, HANDOFF_TIMEOUT):
        raise TimeoutError(message)


@dataclass
class AiResponseGate:
    """AI呼び出しがHTTP境界へ到達したことをテストへ知らせ、テストが許可するまで応答を返さない。"""

    vector: list[float]
    requested: Event = field(default_factory=Event)
    allow_response: Event = field(default_factory=Event)

    async def wait_requested(self, side: str) -> None:
        await _wait_for_signal(self.requested, f"{side}がAI生成へ到達しなかった")

    def release(self) -> None:
        self.allow_response.set()


@pytest.fixture
def gated_ai_responses(gemini_response):
    """N回目のAI呼び出しをN番目に登録したゲートで止め、終了時は全ゲートを開いてAI応答待ちで止まったhandlerスレッドを残さない。"""
    pending: Queue[AiResponseGate] = Queue()
    registered: list[AiResponseGate] = []

    async def respond(request):
        try:
            gate = pending.get_nowait()
        except Empty:
            pytest.fail("用意したゲート数を超えてAI生成が呼ばれた")
        gate.requested.set()
        await _wait_for_signal(gate.allow_response, "AI応答の再開指示が届かなかった")
        return httpx.Response(200, json={"embeddings": [{"values": gate.vector}]})

    gemini_response.side_effect = respond

    def register(vector: list[float]) -> AiResponseGate:
        gate = AiResponseGate(vector)
        registered.append(gate)
        pending.put(gate)
        return gate

    yield register
    for gate in registered:
        gate.release()


@dataclass
class CommitHold:
    """保存UPDATEを終えた接続のpidを記録し、テストが許可するまでコミットへ進ませない。"""

    updated_pids: list[int] = field(default_factory=list)
    update_finished: Event = field(default_factory=Event)
    allow_commit: Event = field(default_factory=Event)

    async def wait_updated(self) -> int:
        await _wait_for_signal(self.update_finished, "先行側のUPDATEが完了しなかった")
        if len(self.updated_pids) != 1:
            pytest.fail("UPDATEまで進んだ接続が1つではなかった")
        return self.updated_pids[0]

    def release(self) -> None:
        self.allow_commit.set()


@pytest.fixture
def commit_hold(monkeypatch):
    """製品のsaveをそのまま実行した直後にCommitHoldで止める。"""
    hold = CommitHold()
    original_save = EmbeddingRepository.save

    async def pause_after_update(self, vector, *, analyzed_article_id):
        saved = await original_save(
            self, vector, analyzed_article_id=analyzed_article_id
        )
        if not saved:
            pytest.fail("コミット前で停止する対象のUPDATEが成功しなかった")
        pid = await self._session.scalar(text("SELECT pg_backend_pid()"))
        if pid is None:
            pytest.fail("保存接続のpidが取得できなかった")
        hold.updated_pids.append(pid)
        hold.update_finished.set()
        await _wait_for_signal(hold.allow_commit, "保存確定の再開指示が届かなかった")
        return saved

    monkeypatch.setattr(EmbeddingRepository, "save", pause_after_update)
    yield hold
    hold.release()


@pytest.fixture
def completion_results(monkeypatch):
    """実Consumerが返した完了種別を記録する。"""
    results: list[EmbeddingCompletion] = []
    original_consume = EmbeddingConsumer.consume

    async def observe(self, event):
        result = await original_consume(self, event)
        results.append(result)
        return result

    monkeypatch.setattr(EmbeddingConsumer, "consume", observe)
    return results
