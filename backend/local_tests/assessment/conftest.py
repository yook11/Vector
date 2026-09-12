# ruff: noqa: S101
"""実Assessmentの外部通信境界と、DB上の障害・待機を制御する。"""

import asyncio
from dataclasses import dataclass, field
from queue import Empty, Queue
from threading import Event
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_providers.deepseek import client as deepseek_module
from app.analysis.assessment.consumer import AssessmentConsumer
from app.analysis.assessment.repository import AssessmentRepository
from app.lambda_handlers.assessment import resources as resource_module
from app.lambda_handlers.assessment.settings import AssessmentConsumerSettings
from app.models.outbox_event import OutboxEvent
from local_tests.assessment.support import deepseek_reply, handler_module
from tests.iam_fixtures import inject_test_db_signer


@pytest.fixture
def deepseek_response():
    return AsyncMock(return_value=deepseek_reply())


@pytest.fixture
def assessment_runtime(system_database, monkeypatch, deepseek_response):
    settings = AssessmentConsumerSettings(
        env="test",
        aws_region="ap-northeast-1",
        database_url=inject_test_db_signer(
            monkeypatch,
            system_database.url("vector_app", sqlalchemy=True),
            resources_module=resource_module,
        ),
        db_iam_auth=True,
        deepseek_api_key_parameter_path="/test/deepseek-key",
    )
    monkeypatch.setattr(handler_module, "AssessmentConsumerSettings", lambda: settings)
    monkeypatch.setattr(
        resource_module,
        "get_secret_parameter",
        Mock(return_value=SecretStr("test-key")),
    )

    def http_factory(**kwargs):
        kwargs.pop("retries")
        return httpx.AsyncClient(  # noqa: TID251
            transport=httpx.MockTransport(deepseek_response), **kwargs
        )

    monkeypatch.setattr(deepseek_module, "make_external_async_client", http_factory)


@dataclass
class DatabaseFailure:
    pending_counts: list[tuple[int, int, int]] = field(default_factory=list)
    errors: list[DBAPIError] = field(default_factory=list)


@pytest.fixture
def database_error_before_commit(monkeypatch):
    """実INSERTで結果・成功監査・Outboxを作った後、同じトランザクションをDBエラーにする。"""
    observation = DatabaseFailure()
    commit = AsyncSession.commit

    async def fail_after_flush(session):
        event = next((row for row in session.new if isinstance(row, OutboxEvent)), None)
        if event is not None:
            await session.flush()
            counts = (
                await session.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM analyzed_articles "
                        "WHERE curation_id=:curation), "
                        "(SELECT count(*) FROM pipeline_events "
                        "WHERE stage='assessment' "
                        "AND event_type='succeeded' "
                        "AND (payload->>'curation_id')::integer=:curation), "
                        "(SELECT count(*) FROM outbox_events "
                        "WHERE event_type='article.assessed_in_scope' "
                        "AND (payload->>'curation_id')::integer=:curation)"
                    ),
                    {"curation": event.payload["curation_id"]},
                )
            ).one()
            observation.pending_counts.append(tuple(counts))
            try:
                await session.execute(text("SELECT 1 / 0"))
            except DBAPIError as exc:
                observation.errors.append(exc)
                raise
        await commit(session)

    monkeypatch.setattr(AsyncSession, "commit", fail_after_flush)
    return observation


async def wait_for_signal(signal, message):
    if not await asyncio.to_thread(signal.wait, 10):
        raise TimeoutError(message)


@dataclass
class AiResponseGate:
    response: httpx.Response
    requested: Event = field(default_factory=Event)
    allow_response: Event = field(default_factory=Event)

    async def wait_requested(self):
        await wait_for_signal(self.requested, "AIリクエストが到達しなかった")

    def release(self):
        self.allow_response.set()


@pytest.fixture
def gated_ai_responses(deepseek_response):
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

    deepseek_response.side_effect = respond

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

    for name in ("save_in_scope", "save_out_of_scope"):
        monkeypatch.setattr(
            AssessmentRepository, name, wrap(getattr(AssessmentRepository, name))
        )
    yield hold
    hold.release()


@pytest.fixture
def completion_results(monkeypatch):
    results = []
    consume = AssessmentConsumer.consume

    async def observe(self, event):
        result = await consume(self, event)
        results.append(result)
        return result

    monkeypatch.setattr(AssessmentConsumer, "consume", observe)
    return results
