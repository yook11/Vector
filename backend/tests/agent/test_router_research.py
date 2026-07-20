"""Research async run API contract tests."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import event as sa_event
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

import app.agent.router as research_router_module
from app.agent.contract import AnswerQuestionResult, AnswerRetrievalSummary
from app.agent.live_updates.stream import AgentRunLiveStreamTerminalEvent
from app.agent.runs.contracts import (
    CancelRunOutcome,
    CancelRunResult,
    DailyQuotaReleaseOutcome,
    DailyRequestLimitExceededError,
)
from app.agent.runs.repository import AgentRunRepository
from app.config import settings
from app.dependencies import get_redis_client
from app.main import app
from app.models.agent_message import AgentMessage, AgentMessageSource
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from app.models.agent_user_daily_quota import AgentUserDailyQuota
from tests.conftest import TEST_ADMIN_ID, TEST_USER_ID

_RESPONSES_URL = "/api/v1/research/responses"
_THREADS_URL = "/api/v1/research/threads"
_QUOTA_USAGE_DATE = date(2026, 7, 20)
_QUOTA_RESET_AT = datetime(
    2026,
    7,
    21,
    tzinfo=timezone(timedelta(hours=9)),
)


class FakeEnqueue:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.calls: list[UUID] = []

    async def __call__(self, run_id: UUID) -> None:
        self.calls.append(run_id)
        if self.exc is not None:
            raise self.exc


class FakeRunEventsRedis:
    def __init__(
        self,
        values: list[str] | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.values = values or []
        self.exc = exc
        self.calls: list[tuple[str, int, int]] = []

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        self.calls.append((key, start, end))
        if self.exc is not None:
            raise self.exc
        return list(self.values)


class FakeCancelStreamPublisher:
    instances: list[FakeCancelStreamPublisher] = []
    raise_on_publish = False

    def __init__(self, redis: object, run_id: UUID, attempt_epoch: int) -> None:
        self.redis = redis
        self.run_id = run_id
        self.attempt_epoch = attempt_epoch
        self.published: list[object] = []
        self.instances.append(self)

    async def publish(self, event: object) -> None:
        self.published.append(event)
        if self.raise_on_publish:
            raise RuntimeError("Redis unavailable")


@pytest.fixture(autouse=True)
def _configured_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deepseek_api_key", SecretStr("deepseek-test-key"))
    monkeypatch.setattr(settings, "tavily_api_key", SecretStr("tvly-test-key"))


@pytest.fixture
async def research_client(
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[tuple[AsyncClient, FakeEnqueue]]:
    async def override_history_session() -> AsyncGenerator[AsyncSession]:
        if db_session.in_transaction():
            await db_session.commit()
        yield db_session

    fake_enqueue = FakeEnqueue()
    app.dependency_overrides[research_router_module.get_agent_persistence_session] = (
        override_history_session
    )
    app.dependency_overrides[get_redis_client] = lambda: FakeRunEventsRedis()
    monkeypatch.setattr(research_router_module, "enqueue_agent_run", fake_enqueue)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=auth_headers,
    ) as client:
        yield client, fake_enqueue
    app.dependency_overrides.clear()


@pytest.fixture
async def quota_research_client(
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[tuple[AsyncClient, FakeEnqueue]]:
    async def override_history_session() -> AsyncGenerator[AsyncSession]:
        if db_session.in_transaction():
            await db_session.commit()
        yield db_session

    fake_enqueue = FakeEnqueue()
    app.dependency_overrides[research_router_module.get_agent_persistence_session] = (
        override_history_session
    )
    monkeypatch.setattr(research_router_module, "enqueue_agent_run", fake_enqueue)
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers=auth_headers,
    ) as client:
        yield client, fake_enqueue
    app.dependency_overrides.clear()


@pytest.fixture
async def anonymous_research_client(
    db_session: AsyncSession,
) -> AsyncGenerator[AsyncClient]:
    async def override_history_session() -> AsyncGenerator[AsyncSession]:
        if db_session.in_transaction():
            await db_session.commit()
        yield db_session

    app.dependency_overrides[research_router_module.get_agent_persistence_session] = (
        override_history_session
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client
    app.dependency_overrides.clear()


async def _fetch_run(session: AsyncSession, run_id: UUID) -> AgentRun:
    run = await session.get(AgentRun, run_id)
    assert run is not None
    return run


async def _create_thread(
    session: AsyncSession,
    *,
    user_id: str = TEST_USER_ID,
    title: str = "既存 thread",
    updated_at: datetime | None = None,
) -> AgentThread:
    thread = AgentThread(
        user_id=UUID(user_id),
        title=title,
    )
    if updated_at is not None:
        thread.updated_at = updated_at
    session.add(thread)
    await session.commit()
    await session.refresh(thread)
    return thread


async def _create_message(
    session: AsyncSession,
    *,
    thread_id: UUID,
    seq: int,
    role: str,
    content: str,
    missing_aspects: list[str] | None = None,
) -> AgentMessage:
    message = AgentMessage(
        thread_id=thread_id,
        seq=seq,
        role=role,
        content=content,
        missing_aspects=missing_aspects or [],
    )
    session.add(message)
    await session.commit()
    await session.refresh(message)
    return message


async def _create_run(
    session: AsyncSession,
    *,
    thread_id: UUID,
    user_message_id: UUID,
    status: str = "queued",
    assistant_message_id: UUID | None = None,
    error_code: str | None = None,
    progress_stage: str | None = None,
    attempt_epoch: int | None = None,
) -> AgentRun:
    run = AgentRun(
        thread_id=thread_id,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        status=status,
        error_code=error_code,
        progress_stage=progress_stage,
    )
    if attempt_epoch is not None:
        run.attempt_epoch = attempt_epoch
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


def _direct_result(answer: str = "worker answer") -> AnswerQuestionResult:
    return AnswerQuestionResult(
        status="answered",
        answer=answer,
        sources=[],
        missing_aspects=[],
        retrieval=AnswerRetrievalSummary(planned_mode="none"),
    )


@pytest.mark.asyncio
class TestCreateResearchResponse:
    async def test_creates_new_thread_user_message_run_and_enqueues(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
    ) -> None:
        client, fake_enqueue = research_client

        response = await client.post(
            _RESPONSES_URL, json={"question": "  NVIDIA の直近動向は？  "}
        )

        assert response.status_code == 202
        data = response.json()
        assert set(data) == {"threadId", "runId"}
        run_id = UUID(data["runId"])
        thread_id = UUID(data["threadId"])
        assert fake_enqueue.calls == [run_id]

        thread = await db_session.get(AgentThread, thread_id)
        assert thread is not None
        assert thread.user_id == UUID(TEST_USER_ID)
        assert thread.title == "NVIDIA の直近動向は？"

        messages = (
            (
                await db_session.execute(
                    select(AgentMessage).where(AgentMessage.thread_id == thread_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(messages) == 1
        assert messages[0].seq == 1
        assert messages[0].role == "user"
        assert messages[0].content == "NVIDIA の直近動向は？"
        assert messages[0].missing_aspects == []

        run = await _fetch_run(db_session, run_id)
        assert run.thread_id == thread_id
        assert run.user_message_id == messages[0].id
        assert run.status == "queued"
        assert run.error_code is None

    @pytest.mark.parametrize("length", [50, 51])
    async def test_new_thread_title_uses_first_50_chars(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
        length: int,
    ) -> None:
        client, _fake_enqueue = research_client
        question = "あ" * length

        response = await client.post(_RESPONSES_URL, json={"question": question})

        assert response.status_code == 202
        thread = await db_session.get(AgentThread, UUID(response.json()["threadId"]))
        assert thread is not None
        assert thread.title == "あ" * 50

    async def test_existing_thread_uses_next_seq_and_bumps_updated_at(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
    ) -> None:
        client, _fake_enqueue = research_client
        old_updated_at = datetime(2026, 1, 1, tzinfo=UTC)
        thread = await _create_thread(db_session, updated_at=old_updated_at)
        await _create_message(
            db_session,
            thread_id=thread.id,
            seq=1,
            role="user",
            content="最初の質問",
        )

        response = await client.post(
            _RESPONSES_URL,
            json={"question": "続きの質問", "threadId": str(thread.id)},
        )

        assert response.status_code == 202
        await db_session.refresh(thread)
        assert thread.updated_at > old_updated_at
        messages = (
            (
                await db_session.execute(
                    select(AgentMessage)
                    .where(AgentMessage.thread_id == thread.id)
                    .order_by(AgentMessage.seq)
                )
            )
            .scalars()
            .all()
        )
        assert [m.seq for m in messages] == [1, 2]
        assert messages[1].content == "続きの質問"

    async def test_active_run_returns_409(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
    ) -> None:
        client, fake_enqueue = research_client
        thread = await _create_thread(db_session)
        user_message = await _create_message(
            db_session,
            thread_id=thread.id,
            seq=1,
            role="user",
            content="実行中の質問",
        )
        await _create_run(
            db_session,
            thread_id=thread.id,
            user_message_id=user_message.id,
            status="running",
        )

        response = await client.post(
            _RESPONSES_URL,
            json={"question": "次の質問", "threadId": str(thread.id)},
        )

        assert response.status_code == 409
        assert response.json() == {
            "detail": "A run is already in progress for this thread"
        }
        assert fake_enqueue.calls == []

    @pytest.mark.parametrize("terminal_status", ["completed", "failed"])
    async def test_terminal_run_allows_next_question(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
        terminal_status: str,
    ) -> None:
        client, fake_enqueue = research_client
        thread = await _create_thread(db_session)
        user_message = await _create_message(
            db_session,
            thread_id=thread.id,
            seq=1,
            role="user",
            content="完了済みの質問",
        )
        assistant_message_id: UUID | None = None
        error_code: str | None = None
        expected_next_seq = 2
        if terminal_status == "completed":
            assistant_message = await _create_message(
                db_session,
                thread_id=thread.id,
                seq=2,
                role="assistant",
                content="完了済みの回答",
            )
            assistant_message_id = assistant_message.id
            expected_next_seq = 3
        else:
            error_code = "internal_error"
        await _create_run(
            db_session,
            thread_id=thread.id,
            user_message_id=user_message.id,
            assistant_message_id=assistant_message_id,
            status=terminal_status,
            error_code=error_code,
        )

        response = await client.post(
            _RESPONSES_URL,
            json={"question": "次の質問", "threadId": str(thread.id)},
        )

        assert response.status_code == 202
        new_run_id = UUID(response.json()["runId"])
        assert fake_enqueue.calls == [new_run_id]
        messages = (
            (
                await db_session.execute(
                    select(AgentMessage)
                    .where(AgentMessage.thread_id == thread.id)
                    .order_by(AgentMessage.seq)
                )
            )
            .scalars()
            .all()
        )
        assert messages[-1].seq == expected_next_seq
        assert messages[-1].role == "user"
        assert messages[-1].content == "次の質問"
        run = await _fetch_run(db_session, new_run_id)
        assert run.status == "queued"
        assert run.user_message_id == messages[-1].id

    async def test_other_users_thread_is_404(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
    ) -> None:
        client, fake_enqueue = research_client
        thread = await _create_thread(db_session, user_id=TEST_ADMIN_ID)

        response = await client.post(
            _RESPONSES_URL,
            json={"question": "横取り", "threadId": str(thread.id)},
        )

        assert response.status_code == 404
        assert fake_enqueue.calls == []

    async def test_enqueue_failure_marks_failed_but_still_returns_run_id(
        self,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def override_history_session() -> AsyncGenerator[AsyncSession]:
            if db_session.in_transaction():
                await db_session.commit()
            yield db_session

        fake_enqueue = FakeEnqueue(exc=RuntimeError("redis down SHOULD_NOT_LEAK"))
        app.dependency_overrides[
            research_router_module.get_agent_persistence_session
        ] = override_history_session
        monkeypatch.setattr(research_router_module, "enqueue_agent_run", fake_enqueue)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers=auth_headers,
        ) as client:
            response = await client.post(
                _RESPONSES_URL, json={"question": "enqueue 失敗する質問"}
            )
        app.dependency_overrides.clear()

        assert response.status_code == 202
        run_id = UUID(response.json()["runId"])
        run = await _fetch_run(db_session, run_id)
        assert run.status == "failed"
        assert run.error_code == "enqueue_failed"
        assert "SHOULD_NOT_LEAK" not in response.text

    async def test_enqueue_failure_rollback_keeps_initial_quota_reservation(
        self,
        quota_research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, fake_enqueue = quota_research_client
        fake_enqueue.exc = RuntimeError("queue unavailable")
        original_mark_enqueue_failed = AgentRunRepository.mark_enqueue_failed

        async def update_then_fail(
            repository: AgentRunRepository,
            run_id: UUID,
            *,
            now: datetime | None = None,
        ) -> bool:
            assert await original_mark_enqueue_failed(repository, run_id, now=now)
            raise RuntimeError("mark failed transaction aborted")

        monkeypatch.setattr(
            AgentRunRepository,
            "mark_enqueue_failed",
            update_then_fail,
        )

        response = await client.post(
            _RESPONSES_URL,
            json={"question": "第2トランザクションが失敗する質問"},
        )

        assert response.status_code == 503
        assert response.json() == {"detail": "Failed to enqueue research run"}
        assert len(fake_enqueue.calls) == 1
        db_session.expire_all()
        runs = (await db_session.execute(select(AgentRun))).scalars().all()
        messages = (await db_session.execute(select(AgentMessage))).scalars().all()
        threads = (await db_session.execute(select(AgentThread))).scalars().all()
        quotas = (await db_session.execute(select(AgentUserDailyQuota))).scalars().all()
        assert len(runs) == 1
        assert (runs[0].status, runs[0].error_code) == ("queued", None)
        assert len(messages) == 1
        assert messages[0].id == runs[0].user_message_id
        assert len(threads) == 1
        assert threads[0].id == runs[0].thread_id
        quota_values = [
            (quota.user_id, quota.usage_date, quota.used_count) for quota in quotas
        ]
        assert quota_values == [(UUID(TEST_USER_ID), runs[0].quota_usage_date, 1)]

    async def test_enqueue_failure_does_not_fail_run_that_already_started(
        self,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def override_history_session() -> AsyncGenerator[AsyncSession]:
            if db_session.in_transaction():
                await db_session.commit()
            yield db_session

        async def enqueue_then_start_and_fail(run_id: UUID) -> None:
            await db_session.execute(
                update(AgentRun).where(AgentRun.id == run_id).values(status="running")
            )
            await db_session.commit()
            raise RuntimeError("redis uncertain SHOULD_NOT_LEAK")

        app.dependency_overrides[
            research_router_module.get_agent_persistence_session
        ] = override_history_session
        monkeypatch.setattr(
            research_router_module, "enqueue_agent_run", enqueue_then_start_and_fail
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers=auth_headers,
        ) as client:
            response = await client.post(
                _RESPONSES_URL, json={"question": "enqueue 失敗 race"}
            )
        app.dependency_overrides.clear()

        assert response.status_code == 202
        run = await _fetch_run(db_session, UUID(response.json()["runId"]))
        assert run.status == "running"
        assert run.error_code is None
        assert "SHOULD_NOT_LEAK" not in response.text

    @pytest.mark.parametrize("missing_key", ["deepseek_api_key", "tavily_api_key"])
    async def test_key_missing_fails_fast_without_persisting_run(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        missing_key: str,
    ) -> None:
        client, fake_enqueue = research_client
        monkeypatch.setattr(settings, missing_key, SecretStr(""))

        response = await client.post(_RESPONSES_URL, json={"question": "NVIDIA は？"})

        assert response.status_code == 503
        assert response.json() == {
            "detail": "Answer generation is temporarily unavailable"
        }
        assert fake_enqueue.calls == []
        runs = (await db_session.execute(select(AgentRun))).scalars().all()
        assert runs == []

    async def test_requires_auth(
        self,
        anonymous_research_client: AsyncClient,
    ) -> None:
        response = await anonymous_research_client.post(
            _RESPONSES_URL, json={"question": "NVIDIA は？"}
        )

        assert response.status_code == 401
        assert response.json() == {"detail": "Not authenticated"}

    @pytest.mark.parametrize("question", ["", "   ", "あ" * 1001])
    async def test_rejects_invalid_question(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        question: str,
    ) -> None:
        client, fake_enqueue = research_client

        response = await client.post(_RESPONSES_URL, json={"question": question})

        assert response.status_code == 422
        assert fake_enqueue.calls == []

    @pytest.mark.parametrize(
        ("decided_at", "expected_retry_after"),
        [
            (datetime(2026, 7, 20, 14, 59, 59, 100_000, tzinfo=UTC), "1"),
            (datetime(2026, 7, 20, 15, 0, 0, 100_000, tzinfo=UTC), "0"),
        ],
    )
    async def test_daily_limit_error_is_flat_typed_429_after_transaction_rollback(
        self,
        quota_research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        decided_at: datetime,
        expected_retry_after: str,
    ) -> None:
        client, fake_enqueue = quota_research_client
        db_session.add(
            AgentUserDailyQuota(
                user_id=UUID(TEST_USER_ID),
                usage_date=_QUOTA_USAGE_DATE,
                used_count=10,
            )
        )
        await db_session.commit()

        async def reject_with_fixed_clock(
            _repository: AgentRunRepository,
            *,
            user_id: UUID,
            question: str,
            thread_id: UUID | None,
            now: datetime | None = None,
        ) -> object:
            assert (user_id, question, thread_id, now) == (
                UUID(TEST_USER_ID),
                "quota rejected",
                None,
                None,
            )
            await db_session.execute(
                update(AgentUserDailyQuota)
                .where(
                    AgentUserDailyQuota.user_id == user_id,
                    AgentUserDailyQuota.usage_date == _QUOTA_USAGE_DATE,
                )
                .values(used_count=9)
            )
            db_session.add(AgentThread(user_id=user_id, title="must roll back"))
            await db_session.flush()
            raise DailyRequestLimitExceededError(
                usage_date=_QUOTA_USAGE_DATE,
                observed_at=decided_at - timedelta(seconds=5),
                decided_at=decided_at,
                limit=10,
            )

        monkeypatch.setattr(
            AgentRunRepository,
            "create_user_run",
            reject_with_fixed_clock,
        )

        response = await client.post(
            _RESPONSES_URL,
            json={"question": "quota rejected"},
        )

        counter = await db_session.scalar(
            select(AgentUserDailyQuota.used_count).where(
                AgentUserDailyQuota.user_id == UUID(TEST_USER_ID),
                AgentUserDailyQuota.usage_date == _QUOTA_USAGE_DATE,
            )
        )
        assert counter == 10
        assert (
            await db_session.scalar(select(func.count()).select_from(AgentThread)) == 0
        )
        assert (
            await db_session.scalar(select(func.count()).select_from(AgentMessage)) == 0
        )
        assert await db_session.scalar(select(func.count()).select_from(AgentRun)) == 0
        assert fake_enqueue.calls == []
        assert response.status_code == 429
        assert response.json() == {
            "detail": "Daily research request limit exceeded",
            "code": "research_daily_request_limit_exceeded",
            "limit": 10,
            "resetAt": _QUOTA_RESET_AT.isoformat(),
        }
        assert response.headers["retry-after"] == expected_retry_after
        assert response.headers["cache-control"] == "no-store"

    async def test_tenth_request_is_accepted_and_eleventh_is_typed_429_without_writes(
        self,
        quota_research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
    ) -> None:
        client, fake_enqueue = quota_research_client

        responses = [
            await client.post(
                _RESPONSES_URL,
                json={"question": f"quota request {index}"},
            )
            for index in range(1, 12)
        ]

        assert (
            await db_session.scalar(select(func.count()).select_from(AgentThread)) == 10
        )
        assert (
            await db_session.scalar(select(func.count()).select_from(AgentMessage))
            == 10
        )
        assert await db_session.scalar(select(func.count()).select_from(AgentRun)) == 10
        quota_rows = (
            (await db_session.execute(select(AgentUserDailyQuota))).scalars().all()
        )
        assert len(quota_rows) == 1
        assert quota_rows[0].used_count == 10
        assert len(fake_enqueue.calls) == 10
        assert [response.status_code for response in responses] == [202] * 10 + [429]
        assert responses[-1].json()["code"] == "research_daily_request_limit_exceeded"
        assert responses[-1].headers["cache-control"] == "no-store"


@pytest.mark.asyncio
class TestQuotaRouterTelemetry:
    @pytest.mark.parametrize(
        ("request_kind", "failing_sink"),
        [
            ("accepted", "log"),
            ("accepted", "metric"),
            ("rejected", "log"),
            ("rejected", "metric"),
        ],
    )
    async def test_admission_telemetry_sink_failure_keeps_response_and_other_sink(
        self,
        quota_research_client: tuple[AsyncClient, FakeEnqueue],
        monkeypatch: pytest.MonkeyPatch,
        request_kind: str,
        failing_sink: str,
    ) -> None:
        client, fake_enqueue = quota_research_client
        log_events: list[str] = []
        metric_results: list[str] = []

        def record_quota_log(event: str, **_kwargs: object) -> None:
            log_events.append(event)
            if failing_sink == "log":
                raise RuntimeError("quota log sink unavailable")

        def record_admission(*, result: str) -> None:
            metric_results.append(result)
            if failing_sink == "metric":
                raise RuntimeError("quota metric sink unavailable")

        monkeypatch.setattr(research_router_module.logger, "info", record_quota_log)
        monkeypatch.setattr(
            research_router_module,
            "record_daily_quota_admission",
            record_admission,
        )
        if request_kind == "rejected":

            async def reject_with_fixed_clock(
                _repository: AgentRunRepository,
                **_kwargs: object,
            ) -> object:
                raise DailyRequestLimitExceededError(
                    usage_date=_QUOTA_USAGE_DATE,
                    observed_at=datetime(2026, 7, 20, 14, 59, tzinfo=UTC),
                    decided_at=datetime(2026, 7, 20, 14, 59, 59, tzinfo=UTC),
                    limit=10,
                )

            monkeypatch.setattr(
                AgentRunRepository,
                "create_user_run",
                reject_with_fixed_clock,
            )

        response = await client.post(
            _RESPONSES_URL,
            json={"question": f"{request_kind} telemetry failure"},
        )

        expected_event = {
            "accepted": "agent_user_daily_quota_reserved",
            "rejected": "agent_user_daily_quota_rejected",
        }[request_kind]
        expected_result = "accepted" if request_kind == "accepted" else "rejected"
        assert log_events == [expected_event]
        assert metric_results == [expected_result]
        if request_kind == "accepted":
            assert response.status_code == 202
            assert fake_enqueue.calls == [UUID(response.json()["runId"])]
        else:
            assert response.status_code == 429
            assert response.json() == {
                "detail": "Daily research request limit exceeded",
                "code": "research_daily_request_limit_exceeded",
                "limit": 10,
                "resetAt": "2026-07-21T00:00:00+09:00",
            }
            assert response.headers["retry-after"] == "1"

    @pytest.mark.parametrize("failing_sink", ["log", "metric"])
    async def test_queued_release_telemetry_sink_failure_keeps_cancelled_response(
        self,
        quota_research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        failing_sink: str,
    ) -> None:
        client, _fake_enqueue = quota_research_client
        thread = await _create_thread(db_session)
        message = await _create_message(
            db_session,
            thread_id=thread.id,
            seq=1,
            role="user",
            content="queued quota release",
        )
        run = await _create_run(
            db_session,
            thread_id=thread.id,
            user_message_id=message.id,
        )
        run_id = run.id
        run.quota_usage_date = _QUOTA_USAGE_DATE
        db_session.add(
            AgentUserDailyQuota(
                user_id=UUID(TEST_USER_ID),
                usage_date=_QUOTA_USAGE_DATE,
                used_count=1,
            )
        )
        await db_session.commit()
        log_events: list[str] = []
        metric_results: list[str] = []

        def record_quota_log(event: str, **_kwargs: object) -> None:
            log_events.append(event)
            if failing_sink == "log":
                raise RuntimeError("quota log sink unavailable")

        def record_release(*, result: str) -> None:
            metric_results.append(result)
            if failing_sink == "metric":
                raise RuntimeError("quota metric sink unavailable")

        monkeypatch.setattr(research_router_module.logger, "info", record_quota_log)
        monkeypatch.setattr(
            research_router_module,
            "record_daily_quota_release",
            record_release,
        )

        response = await client.post(f"/api/v1/research/runs/{run_id}/cancel")

        assert response.status_code == 204
        assert log_events == ["agent_user_daily_quota_released"]
        assert metric_results == ["released"]
        db_session.expire_all()
        persisted = await _fetch_run(db_session, run_id)
        assert (persisted.status, persisted.error_code) == ("failed", "cancelled")
        assert (
            await db_session.scalar(
                select(AgentUserDailyQuota.used_count).where(
                    AgentUserDailyQuota.user_id == UUID(TEST_USER_ID),
                    AgentUserDailyQuota.usage_date == _QUOTA_USAGE_DATE,
                )
            )
            == 0
        )

    async def test_running_not_eligible_metric_failure_keeps_terminal_publish(
        self,
        quota_research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _fake_enqueue = quota_research_client
        thread = await _create_thread(db_session)
        message = await _create_message(
            db_session,
            thread_id=thread.id,
            seq=1,
            role="user",
            content="running quota not eligible",
        )
        run = await _create_run(
            db_session,
            thread_id=thread.id,
            user_message_id=message.id,
            status="running",
            attempt_epoch=3,
        )
        run_id = run.id
        run.quota_usage_date = _QUOTA_USAGE_DATE
        db_session.add(
            AgentUserDailyQuota(
                user_id=UUID(TEST_USER_ID),
                usage_date=_QUOTA_USAGE_DATE,
                used_count=1,
            )
        )
        await db_session.commit()
        metric_results: list[str] = []
        FakeCancelStreamPublisher.instances = []

        def fail_record_release(*, result: str) -> None:
            metric_results.append(result)
            raise RuntimeError("quota metric sink unavailable")

        monkeypatch.setattr(
            research_router_module,
            "record_daily_quota_release",
            fail_record_release,
        )
        monkeypatch.setattr(
            research_router_module,
            "AgentRunLiveStreamPublisher",
            FakeCancelStreamPublisher,
        )
        app.dependency_overrides[get_redis_client] = lambda: FakeRunEventsRedis()

        response = await client.post(f"/api/v1/research/runs/{run_id}/cancel")

        assert response.status_code == 204
        assert metric_results == ["not_eligible"]
        assert len(FakeCancelStreamPublisher.instances) == 1
        assert FakeCancelStreamPublisher.instances[0].published == [
            AgentRunLiveStreamTerminalEvent(
                status="failed",
                errorCode="cancelled",
            )
        ]
        db_session.expire_all()
        persisted = await _fetch_run(db_session, run_id)
        assert (persisted.status, persisted.error_code) == ("failed", "cancelled")
        assert (
            await db_session.scalar(
                select(AgentUserDailyQuota.used_count).where(
                    AgentUserDailyQuota.user_id == UUID(TEST_USER_ID),
                    AgentUserDailyQuota.usage_date == _QUOTA_USAGE_DATE,
                )
            )
            == 1
        )

    async def test_accepted_request_records_safe_quota_admission_after_commit(
        self,
        quota_research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _fake_enqueue = quota_research_client
        calls: list[dict[str, str]] = []
        sensitive_question = "quota telemetry secret question"

        def record_admission(*, result: str) -> None:
            calls.append({"result": result})

        monkeypatch.setattr(
            research_router_module,
            "record_daily_quota_admission",
            record_admission,
            raising=False,
        )

        with capture_logs() as logs:
            response = await client.post(
                _RESPONSES_URL,
                json={"question": sensitive_question},
            )

        run_id = UUID(response.json()["runId"])
        run = await _fetch_run(db_session, run_id)
        reserved = [
            entry
            for entry in logs
            if entry.get("event") == "agent_user_daily_quota_reserved"
        ]
        assert response.status_code == 202
        assert calls == [{"result": "accepted"}]
        assert reserved == [
            {
                "run_id": str(run_id),
                "usage_date": run.quota_usage_date.isoformat(),
                "used_count": 1,
                "limit": 10,
                "event": "agent_user_daily_quota_reserved",
                "log_level": "info",
            }
        ]
        serialized_logs = repr(logs)
        assert sensitive_question not in serialized_logs
        assert TEST_USER_ID not in serialized_logs

    async def test_rejected_request_records_safe_quota_admission_after_rollback(
        self,
        quota_research_client: tuple[AsyncClient, FakeEnqueue],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _fake_enqueue = quota_research_client
        calls: list[dict[str, str]] = []
        sensitive_question = "quota rejection secret question"

        async def reject_with_fixed_clock(
            _repository: AgentRunRepository,
            **_kwargs: object,
        ) -> object:
            raise DailyRequestLimitExceededError(
                usage_date=_QUOTA_USAGE_DATE,
                observed_at=datetime(2026, 7, 20, 14, 59, tzinfo=UTC),
                decided_at=datetime(2026, 7, 20, 14, 59, 59, tzinfo=UTC),
                limit=10,
            )

        def record_admission(*, result: str) -> None:
            calls.append({"result": result})

        monkeypatch.setattr(
            AgentRunRepository,
            "create_user_run",
            reject_with_fixed_clock,
        )
        monkeypatch.setattr(
            research_router_module,
            "record_daily_quota_admission",
            record_admission,
            raising=False,
        )

        with capture_logs() as logs:
            response = await client.post(
                _RESPONSES_URL,
                json={"question": sensitive_question},
            )

        rejected = [
            entry
            for entry in logs
            if entry.get("event") == "agent_user_daily_quota_rejected"
        ]
        assert response.status_code == 429
        assert calls == [{"result": "rejected"}]
        assert rejected == [
            {
                "usage_date": _QUOTA_USAGE_DATE.isoformat(),
                "limit": 10,
                "event": "agent_user_daily_quota_rejected",
                "log_level": "info",
            }
        ]
        serialized_logs = repr(logs)
        assert sensitive_question not in serialized_logs
        assert TEST_USER_ID not in serialized_logs

    async def test_create_transaction_failure_does_not_record_quota_admission(
        self,
        quota_research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _fake_enqueue = quota_research_client
        calls: list[dict[str, str]] = []
        commit_attempted = False

        def fail_commit(_session: object) -> None:
            nonlocal commit_attempted
            commit_attempted = True
            raise RuntimeError("database commit failure")

        def record_admission(*, result: str) -> None:
            calls.append({"result": result})

        monkeypatch.setattr(
            research_router_module,
            "record_daily_quota_admission",
            record_admission,
            raising=False,
        )
        sa_event.listen(
            db_session.sync_session, "before_commit", fail_commit, once=True
        )

        with capture_logs() as logs:
            response = await client.post(
                _RESPONSES_URL,
                json={"question": "transaction failure"},
            )

        assert response.status_code == 500
        assert commit_attempted is True
        assert calls == []
        assert not [
            entry
            for entry in logs
            if entry.get("event")
            in {
                "agent_user_daily_quota_reserved",
                "agent_user_daily_quota_rejected",
            }
        ]
        if db_session.in_transaction():
            await db_session.rollback()
        assert await db_session.scalar(select(func.count()).select_from(AgentRun)) == 0
        assert (
            await db_session.scalar(
                select(func.count()).select_from(AgentUserDailyQuota)
            )
            == 0
        )

    @pytest.mark.parametrize(
        ("release_outcome", "quota_usage_date", "quota_used_count", "event"),
        [
            (
                DailyQuotaReleaseOutcome.RELEASED,
                _QUOTA_USAGE_DATE,
                0,
                "agent_user_daily_quota_released",
            ),
            (
                DailyQuotaReleaseOutcome.INCONSISTENT,
                _QUOTA_USAGE_DATE,
                None,
                "agent_user_daily_quota_release_inconsistent",
            ),
            (DailyQuotaReleaseOutcome.NOT_ELIGIBLE, None, None, None),
        ],
    )
    async def test_cancel_records_quota_release_result_after_commit(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        monkeypatch: pytest.MonkeyPatch,
        release_outcome: DailyQuotaReleaseOutcome,
        quota_usage_date: date | None,
        quota_used_count: int | None,
        event: str | None,
    ) -> None:
        client, _fake_enqueue = research_client
        run_id = UUID("00000000-0000-4000-a000-000000000071")
        calls: list[dict[str, str]] = []
        outcome = CancelRunResult(
            outcome=CancelRunOutcome.CANCELLED,
            attempt_epoch=0,
            quota_release_outcome=release_outcome,
            quota_usage_date=quota_usage_date,
            quota_used_count=quota_used_count,
        )

        async def cancel_with_fixed_result(
            _repository: AgentRunRepository,
            **_kwargs: object,
        ) -> CancelRunResult:
            return outcome

        def record_release(*, result: str) -> None:
            calls.append({"result": result})

        monkeypatch.setattr(
            AgentRunRepository,
            "cancel_run_for_user",
            cancel_with_fixed_result,
        )
        monkeypatch.setattr(
            research_router_module,
            "record_daily_quota_release",
            record_release,
            raising=False,
        )

        with capture_logs() as logs:
            response = await client.post(f"/api/v1/research/runs/{run_id}/cancel")

        quota_events = [
            entry
            for entry in logs
            if entry.get("event", "").startswith("agent_user_daily_quota_")
        ]
        assert response.status_code == 204
        assert calls == [{"result": release_outcome.value}]
        if release_outcome is DailyQuotaReleaseOutcome.RELEASED:
            assert quota_events == [
                {
                    "run_id": str(run_id),
                    "usage_date": _QUOTA_USAGE_DATE.isoformat(),
                    "used_count": 0,
                    "limit": 10,
                    "event": "agent_user_daily_quota_released",
                    "log_level": "info",
                }
            ]
        elif release_outcome is DailyQuotaReleaseOutcome.INCONSISTENT:
            assert quota_events == [
                {
                    "run_id": str(run_id),
                    "usage_date": _QUOTA_USAGE_DATE.isoformat(),
                    "limit": 10,
                    "event": "agent_user_daily_quota_release_inconsistent",
                    "log_level": "error",
                }
            ]
        else:
            assert event is None
            assert quota_events == []
        assert TEST_USER_ID not in repr(logs)

    async def test_cancel_release_commit_failure_records_no_quota_telemetry(
        self,
        quota_research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _fake_enqueue = quota_research_client
        thread = await _create_thread(db_session)
        message = await _create_message(
            db_session,
            thread_id=thread.id,
            seq=1,
            role="user",
            content="release then commit failure",
        )
        run = await _create_run(
            db_session,
            thread_id=thread.id,
            user_message_id=message.id,
        )
        run_id = run.id
        run.quota_usage_date = _QUOTA_USAGE_DATE
        db_session.add(
            AgentUserDailyQuota(
                user_id=UUID(TEST_USER_ID),
                usage_date=_QUOTA_USAGE_DATE,
                used_count=1,
            )
        )
        await db_session.commit()

        calls: list[dict[str, str]] = []
        commit_attempted = False

        def fail_commit(_session: object) -> None:
            nonlocal commit_attempted
            commit_attempted = True
            raise RuntimeError("cancel commit failure")

        def record_release(*, result: str) -> None:
            calls.append({"result": result})

        monkeypatch.setattr(
            research_router_module,
            "record_daily_quota_release",
            record_release,
            raising=False,
        )
        sa_event.listen(
            db_session.sync_session, "before_commit", fail_commit, once=True
        )

        with capture_logs() as logs:
            response = await client.post(f"/api/v1/research/runs/{run_id}/cancel")

        assert response.status_code == 500
        assert commit_attempted is True
        assert calls == []
        assert not [
            entry
            for entry in logs
            if entry.get("event")
            in {
                "agent_user_daily_quota_released",
                "agent_user_daily_quota_release_inconsistent",
            }
        ]

        if db_session.in_transaction():
            await db_session.rollback()
        db_session.expire_all()
        persisted_run = await db_session.get(AgentRun, run_id)
        assert persisted_run is not None
        assert (persisted_run.status, persisted_run.error_code) == ("queued", None)
        assert (
            await db_session.scalar(
                select(AgentUserDailyQuota.used_count).where(
                    AgentUserDailyQuota.user_id == UUID(TEST_USER_ID),
                    AgentUserDailyQuota.usage_date == _QUOTA_USAGE_DATE,
                )
            )
            == 1
        )

    @pytest.mark.parametrize(
        ("outcome", "expected_status"),
        [
            (CancelRunOutcome.ALREADY_FAILED, 204),
            (CancelRunOutcome.ALREADY_COMPLETED, 409),
        ],
    )
    async def test_cancel_terminal_result_does_not_record_quota_release(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        monkeypatch: pytest.MonkeyPatch,
        outcome: CancelRunOutcome,
        expected_status: int,
    ) -> None:
        client, _fake_enqueue = research_client
        calls: list[dict[str, str]] = []

        async def cancel_as_terminal(
            _repository: AgentRunRepository,
            **_kwargs: object,
        ) -> CancelRunResult:
            return CancelRunResult(outcome=outcome)

        def record_release(*, result: str) -> None:
            calls.append({"result": result})

        monkeypatch.setattr(
            AgentRunRepository,
            "cancel_run_for_user",
            cancel_as_terminal,
        )
        monkeypatch.setattr(
            research_router_module,
            "record_daily_quota_release",
            record_release,
            raising=False,
        )

        with capture_logs() as logs:
            response = await client.post(
                "/api/v1/research/runs/00000000-0000-4000-a000-000000000072/cancel"
            )

        assert response.status_code == expected_status
        assert calls == []
        assert not [
            entry
            for entry in logs
            if entry.get("event", "").startswith("agent_user_daily_quota_")
        ]


@pytest.mark.asyncio
class TestListResearchThreads:
    async def test_lists_only_own_threads_by_updated_at_with_default_page_size(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
    ) -> None:
        client, _fake_enqueue = research_client
        base = datetime(2026, 7, 1, tzinfo=UTC)
        threads = [
            await _create_thread(
                db_session,
                title=f"own-{index}",
                updated_at=base.replace(day=index + 1),
            )
            for index in range(22)
        ]
        active_user = await _create_message(
            db_session,
            thread_id=threads[21].id,
            seq=1,
            role="user",
            content="active",
        )
        await _create_run(
            db_session,
            thread_id=threads[21].id,
            user_message_id=active_user.id,
            status="running",
        )
        terminal_user = await _create_message(
            db_session,
            thread_id=threads[20].id,
            seq=1,
            role="user",
            content="terminal",
        )
        await _create_run(
            db_session,
            thread_id=threads[20].id,
            user_message_id=terminal_user.id,
            status="failed",
            error_code="internal_error",
        )
        await _create_thread(
            db_session,
            user_id=TEST_ADMIN_ID,
            title="other-user-newer",
            updated_at=datetime(2026, 7, 31, tzinfo=UTC),
        )

        response = await client.get(_THREADS_URL)

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 22
        assert data["page"] == 1
        assert data["perPage"] == 20
        assert data["totalPages"] == 2
        assert len(data["items"]) == 20
        assert data["items"][0] == {
            "threadId": str(threads[21].id),
            "title": "own-21",
            "updatedAt": "2026-07-22T00:00:00Z",
            "hasActiveRun": True,
        }
        assert data["items"][1]["threadId"] == str(threads[20].id)
        assert data["items"][1]["hasActiveRun"] is False
        assert all(item["title"] != "other-user-newer" for item in data["items"])

    async def test_list_empty_threads_returns_200(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
    ) -> None:
        client, _fake_enqueue = research_client

        response = await client.get(_THREADS_URL)

        assert response.status_code == 200
        assert response.json() == {
            "items": [],
            "total": 0,
            "page": 1,
            "perPage": 20,
            "totalPages": 0,
        }

    async def test_list_rejects_per_page_over_100(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
    ) -> None:
        client, _fake_enqueue = research_client

        response = await client.get(_THREADS_URL, params={"perPage": 101})

        assert response.status_code == 422


@pytest.mark.asyncio
class TestGetResearchThread:
    async def test_returns_thread_detail_as_message_discriminated_union(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
    ) -> None:
        client, _fake_enqueue = research_client
        thread = await _create_thread(db_session, title="AI 半導体")
        active_user = await _create_message(
            db_session,
            thread_id=thread.id,
            seq=1,
            role="user",
            content="実行中の質問",
        )
        active_run = await _create_run(
            db_session,
            thread_id=thread.id,
            user_message_id=active_user.id,
            status="queued",
            progress_stage=None,
        )
        answered_user = await _create_message(
            db_session,
            thread_id=thread.id,
            seq=2,
            role="user",
            content="回答済みの質問",
        )
        assistant_message = await _create_message(
            db_session,
            thread_id=thread.id,
            seq=3,
            role="assistant",
            content="回答です。[[1]][[2]]",
            missing_aspects=["未確認の観点"],
        )
        db_session.add_all(
            [
                AgentMessageSource(
                    message_id=assistant_message.id,
                    ordinal=1,
                    kind="internal_article",
                    source_ref="1",
                    analyzed_article_id=None,
                    title="削除済み内部記事",
                    published_at=datetime(2026, 7, 1, tzinfo=UTC),
                ),
                AgentMessageSource(
                    message_id=assistant_message.id,
                    ordinal=2,
                    kind="external_url",
                    source_ref="2",
                    url="https://example.com/source",
                    title="External source",
                    source_name="Example",
                    published_at=None,
                    evidence_claim="External claim.",
                ),
            ]
        )
        await db_session.commit()
        completed_run = await _create_run(
            db_session,
            thread_id=thread.id,
            user_message_id=answered_user.id,
            assistant_message_id=assistant_message.id,
            status="completed",
            progress_stage="synthesizing",
        )
        cancelled_user = await _create_message(
            db_session,
            thread_id=thread.id,
            seq=4,
            role="user",
            content="止めた質問",
        )
        cancelled_run = await _create_run(
            db_session,
            thread_id=thread.id,
            user_message_id=cancelled_user.id,
            status="failed",
            error_code="cancelled",
        )

        response = await client.get(f"{_THREADS_URL}/{thread.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["threadId"] == str(thread.id)
        assert data["title"] == "AI 半導体"
        assert [message["role"] for message in data["messages"]] == [
            "user",
            "user",
            "assistant",
            "user",
        ]
        assert [message["seq"] for message in data["messages"]] == [1, 2, 3, 4]
        assert data["messages"][0]["run"] == {
            "runId": str(active_run.id),
            "status": "queued",
            "errorCode": None,
            "progressStage": None,
        }
        assert data["messages"][1]["run"] == {
            "runId": str(completed_run.id),
            "status": "completed",
            "errorCode": None,
            "progressStage": "synthesizing",
        }
        assert data["messages"][2]["content"] == "回答です。[[1]][[2]]"
        assert data["messages"][2]["missingAspects"] == ["未確認の観点"]
        assert data["messages"][2]["sources"] == [
            {
                "kind": "internal_article",
                "sourceRef": "1",
                "articleId": None,
                "title": "削除済み内部記事",
                "publishedAt": "2026-07-01T00:00:00Z",
            },
            {
                "kind": "external_url",
                "sourceRef": "2",
                "url": "https://example.com/source",
                "title": "External source",
                "sourceName": "Example",
                "publishedAt": None,
                "evidenceClaim": "External claim.",
            },
        ]
        assert data["messages"][3]["run"] == {
            "runId": str(cancelled_run.id),
            "status": "failed",
            "errorCode": "cancelled",
            "progressStage": None,
        }
        assert all("createdAt" in message for message in data["messages"])

    async def test_thread_detail_other_user_is_404(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
    ) -> None:
        client, _fake_enqueue = research_client
        thread = await _create_thread(db_session, user_id=TEST_ADMIN_ID)

        response = await client.get(f"{_THREADS_URL}/{thread.id}")

        assert response.status_code == 404


@pytest.mark.asyncio
class TestDeleteResearchThread:
    async def test_deletes_thread_and_cascades_history_rows(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
    ) -> None:
        client, _fake_enqueue = research_client
        thread = await _create_thread(db_session)
        user_message = await _create_message(
            db_session, thread_id=thread.id, seq=1, role="user", content="question"
        )
        assistant_message = await _create_message(
            db_session, thread_id=thread.id, seq=2, role="assistant", content="answer"
        )
        db_session.add(
            AgentMessageSource(
                message_id=assistant_message.id,
                ordinal=1,
                kind="external_url",
                source_ref="1",
                url="https://example.com/source",
                title="External source",
                evidence_claim="External claim.",
            )
        )
        await db_session.commit()
        await _create_run(
            db_session,
            thread_id=thread.id,
            user_message_id=user_message.id,
            assistant_message_id=assistant_message.id,
            status="completed",
        )
        thread_id = thread.id

        response = await client.delete(f"{_THREADS_URL}/{thread_id}")

        assert response.status_code == 204
        db_session.expire_all()
        assert (
            await db_session.scalar(
                select(func.count())
                .select_from(AgentThread)
                .where(AgentThread.id == thread_id)
            )
            == 0
        )
        assert (
            await db_session.scalar(select(func.count()).select_from(AgentMessage)) == 0
        )
        assert await db_session.scalar(select(func.count()).select_from(AgentRun)) == 0
        assert (
            await db_session.scalar(
                select(func.count()).select_from(AgentMessageSource)
            )
            == 0
        )
        second_response = await client.delete(f"{_THREADS_URL}/{thread_id}")
        assert second_response.status_code == 404

    async def test_active_run_delete_makes_worker_completion_harmless(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
    ) -> None:
        client, _fake_enqueue = research_client
        thread = await _create_thread(db_session)
        user_message = await _create_message(
            db_session, thread_id=thread.id, seq=1, role="user", content="active"
        )
        run = await _create_run(
            db_session,
            thread_id=thread.id,
            user_message_id=user_message.id,
            status="running",
        )
        run_id = run.id
        expected_attempt_epoch = run.attempt_epoch

        response = await client.delete(f"{_THREADS_URL}/{thread.id}")

        assert response.status_code == 204
        if db_session.in_transaction():
            await db_session.commit()
        db_session.expire_all()
        async with db_session.begin():
            completed = await AgentRunRepository(db_session).complete_run(
                run_id=run_id,
                result=_direct_result(),
                expected_attempt_epoch=expected_attempt_epoch,
            )
        assert completed is False
        assert await db_session.scalar(select(func.count()).select_from(AgentRun)) == 0
        assert (
            await db_session.scalar(select(func.count()).select_from(AgentMessage)) == 0
        )
        assert (
            await db_session.scalar(
                select(func.count()).select_from(AgentMessageSource)
            )
            == 0
        )

    async def test_delete_other_user_thread_is_404(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
    ) -> None:
        client, _fake_enqueue = research_client
        thread = await _create_thread(db_session, user_id=TEST_ADMIN_ID)

        response = await client.delete(f"{_THREADS_URL}/{thread.id}")

        assert response.status_code == 404


@pytest.mark.asyncio
class TestCancelResearchRun:
    @pytest.mark.parametrize("initial_status", ["queued", "running"])
    async def test_cancel_active_run_marks_cancelled(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
        initial_status: str,
    ) -> None:
        client, _fake_enqueue = research_client
        thread = await _create_thread(db_session)
        user_message = await _create_message(
            db_session, thread_id=thread.id, seq=1, role="user", content="active"
        )
        run = await _create_run(
            db_session,
            thread_id=thread.id,
            user_message_id=user_message.id,
            status=initial_status,
        )

        response = await client.post(f"/api/v1/research/runs/{run.id}/cancel")

        assert response.status_code == 204
        await db_session.refresh(run)
        assert run.status == "failed"
        assert run.error_code == "cancelled"

    async def test_cancel_running_run_publishes_terminal_after_commit(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _fake_enqueue = research_client
        thread = await _create_thread(db_session)
        user_message = await _create_message(
            db_session, thread_id=thread.id, seq=1, role="user", content="running"
        )
        run = await _create_run(
            db_session,
            thread_id=thread.id,
            user_message_id=user_message.id,
            status="running",
            attempt_epoch=3,
        )
        FakeCancelStreamPublisher.instances = []

        class CommitCheckingPublisher(FakeCancelStreamPublisher):
            async def publish(self, event: object) -> None:
                assert not db_session.in_transaction()
                await super().publish(event)

        monkeypatch.setattr(
            research_router_module,
            "AgentRunLiveStreamPublisher",
            CommitCheckingPublisher,
            raising=False,
        )

        response = await client.post(f"/api/v1/research/runs/{run.id}/cancel")

        assert response.status_code == 204
        assert len(FakeCancelStreamPublisher.instances) == 1
        publisher = FakeCancelStreamPublisher.instances[0]
        assert publisher.run_id == run.id
        assert publisher.attempt_epoch == 3
        assert publisher.published == [
            AgentRunLiveStreamTerminalEvent(
                status="failed",
                errorCode="cancelled",
            )
        ]

    async def test_cancel_queued_epoch_zero_does_not_create_stream_publisher(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _fake_enqueue = research_client
        thread = await _create_thread(db_session)
        user_message = await _create_message(
            db_session, thread_id=thread.id, seq=1, role="user", content="queued"
        )
        run = await _create_run(
            db_session,
            thread_id=thread.id,
            user_message_id=user_message.id,
            status="queued",
            attempt_epoch=0,
        )
        FakeCancelStreamPublisher.instances = []
        monkeypatch.setattr(
            research_router_module,
            "AgentRunLiveStreamPublisher",
            FakeCancelStreamPublisher,
            raising=False,
        )

        response = await client.post(f"/api/v1/research/runs/{run.id}/cancel")

        assert response.status_code == 204
        assert FakeCancelStreamPublisher.instances == []

    async def test_cancel_terminal_publish_failure_preserves_204(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _fake_enqueue = research_client
        thread = await _create_thread(db_session)
        user_message = await _create_message(
            db_session, thread_id=thread.id, seq=1, role="user", content="running"
        )
        run = await _create_run(
            db_session,
            thread_id=thread.id,
            user_message_id=user_message.id,
            status="running",
            attempt_epoch=2,
        )
        FakeCancelStreamPublisher.instances = []
        monkeypatch.setattr(FakeCancelStreamPublisher, "raise_on_publish", True)
        monkeypatch.setattr(
            research_router_module,
            "AgentRunLiveStreamPublisher",
            FakeCancelStreamPublisher,
            raising=False,
        )

        response = await client.post(f"/api/v1/research/runs/{run.id}/cancel")

        assert response.status_code == 204
        assert any(
            isinstance(event, AgentRunLiveStreamTerminalEvent)
            for event in FakeCancelStreamPublisher.instances[0].published
        )
        await db_session.refresh(run)
        assert run.status == "failed"
        assert run.error_code == "cancelled"

    async def test_cancel_completed_run_returns_409_and_preserves_completed(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _fake_enqueue = research_client
        thread = await _create_thread(db_session)
        user_message = await _create_message(
            db_session, thread_id=thread.id, seq=1, role="user", content="done"
        )
        assistant_message = await _create_message(
            db_session, thread_id=thread.id, seq=2, role="assistant", content="answer"
        )
        run = await _create_run(
            db_session,
            thread_id=thread.id,
            user_message_id=user_message.id,
            assistant_message_id=assistant_message.id,
            status="completed",
        )
        FakeCancelStreamPublisher.instances = []
        monkeypatch.setattr(
            research_router_module,
            "AgentRunLiveStreamPublisher",
            FakeCancelStreamPublisher,
            raising=False,
        )

        response = await client.post(f"/api/v1/research/runs/{run.id}/cancel")

        assert response.status_code == 409
        assert response.json() == {"detail": "Run already completed"}
        await db_session.refresh(run)
        assert run.status == "completed"
        assert run.error_code is None
        assert FakeCancelStreamPublisher.instances == []

    async def test_cancel_failed_run_is_204_and_preserves_error_code(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _fake_enqueue = research_client
        thread = await _create_thread(db_session)
        user_message = await _create_message(
            db_session, thread_id=thread.id, seq=1, role="user", content="failed"
        )
        run = await _create_run(
            db_session,
            thread_id=thread.id,
            user_message_id=user_message.id,
            status="failed",
            error_code="internal_error",
        )
        FakeCancelStreamPublisher.instances = []
        monkeypatch.setattr(
            research_router_module,
            "AgentRunLiveStreamPublisher",
            FakeCancelStreamPublisher,
            raising=False,
        )

        response = await client.post(f"/api/v1/research/runs/{run.id}/cancel")

        assert response.status_code == 204
        await db_session.refresh(run)
        assert run.status == "failed"
        assert run.error_code == "internal_error"
        assert FakeCancelStreamPublisher.instances == []

    async def test_cancel_other_users_run_is_404(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _fake_enqueue = research_client
        thread = await _create_thread(db_session, user_id=TEST_ADMIN_ID)
        user_message = await _create_message(
            db_session, thread_id=thread.id, seq=1, role="user", content="other"
        )
        run = await _create_run(
            db_session, thread_id=thread.id, user_message_id=user_message.id
        )
        FakeCancelStreamPublisher.instances = []
        monkeypatch.setattr(
            research_router_module,
            "AgentRunLiveStreamPublisher",
            FakeCancelStreamPublisher,
            raising=False,
        )

        response = await client.post(f"/api/v1/research/runs/{run.id}/cancel")

        assert response.status_code == 404
        assert FakeCancelStreamPublisher.instances == []

    async def test_cancel_unknown_run_is_404_without_terminal(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _fake_enqueue = research_client
        FakeCancelStreamPublisher.instances = []
        monkeypatch.setattr(
            research_router_module,
            "AgentRunLiveStreamPublisher",
            FakeCancelStreamPublisher,
            raising=False,
        )

        response = await client.post(
            "/api/v1/research/runs/00000000-0000-4000-a000-000000000099/cancel"
        )

        assert response.status_code == 404
        assert FakeCancelStreamPublisher.instances == []

    async def test_cancelled_run_is_not_overwritten_by_worker_completion(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
    ) -> None:
        client, _fake_enqueue = research_client
        thread = await _create_thread(db_session)
        user_message = await _create_message(
            db_session, thread_id=thread.id, seq=1, role="user", content="active"
        )
        run = await _create_run(
            db_session,
            thread_id=thread.id,
            user_message_id=user_message.id,
            status="running",
        )
        run_id = run.id
        expected_attempt_epoch = run.attempt_epoch
        thread_id = thread.id

        response = await client.post(f"/api/v1/research/runs/{run_id}/cancel")

        assert response.status_code == 204
        if db_session.in_transaction():
            await db_session.commit()
        db_session.expire_all()
        async with db_session.begin():
            completed = await AgentRunRepository(db_session).complete_run(
                run_id=run_id,
                result=_direct_result(),
                expected_attempt_epoch=expected_attempt_epoch,
            )
        refreshed_run = await db_session.get(AgentRun, run_id)
        assert refreshed_run is not None
        assert completed is False
        assert refreshed_run.status == "failed"
        assert refreshed_run.error_code == "cancelled"
        messages = (
            (
                await db_session.execute(
                    select(AgentMessage)
                    .where(AgentMessage.thread_id == thread_id)
                    .order_by(AgentMessage.seq)
                )
            )
            .scalars()
            .all()
        )
        assert [message.role for message in messages] == ["user"]


@pytest.mark.asyncio
class TestGetResearchRun:
    async def test_returns_slim_signal_for_all_run_statuses(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
    ) -> None:
        client, _fake_enqueue = research_client
        queued_thread = await _create_thread(db_session)
        queued_user = await _create_message(
            db_session,
            thread_id=queued_thread.id,
            seq=1,
            role="user",
            content="queued?",
        )
        queued_run = await _create_run(
            db_session, thread_id=queued_thread.id, user_message_id=queued_user.id
        )
        running_thread = await _create_thread(db_session)
        running_user = await _create_message(
            db_session,
            thread_id=running_thread.id,
            seq=1,
            role="user",
            content="running?",
        )
        running_run = await _create_run(
            db_session,
            thread_id=running_thread.id,
            user_message_id=running_user.id,
            status="running",
            progress_stage="retrieving",
            attempt_epoch=3,
        )
        failed_thread = await _create_thread(db_session)
        failed_user = await _create_message(
            db_session,
            thread_id=failed_thread.id,
            seq=1,
            role="user",
            content="failed?",
        )
        failed_run = await _create_run(
            db_session,
            thread_id=failed_thread.id,
            user_message_id=failed_user.id,
            status="failed",
            error_code="generation_unavailable",
            progress_stage="synthesizing",
            attempt_epoch=2,
        )
        completed_thread = await _create_thread(db_session)
        completed_user = await _create_message(
            db_session,
            thread_id=completed_thread.id,
            seq=1,
            role="user",
            content="completed?",
        )
        completed_message = await _create_message(
            db_session,
            thread_id=completed_thread.id,
            seq=2,
            role="assistant",
            content="completed answer",
        )
        completed_run = await _create_run(
            db_session,
            thread_id=completed_thread.id,
            user_message_id=completed_user.id,
            assistant_message_id=completed_message.id,
            status="completed",
            attempt_epoch=2,
        )

        expected = {
            queued_run.id: ("queued", None, None, 0),
            running_run.id: ("running", None, "retrieving", 3),
            failed_run.id: ("failed", "generation_unavailable", "synthesizing", 2),
            completed_run.id: ("completed", None, None, 2),
        }
        for run_id, (
            status_value,
            error_code,
            progress_stage,
            attempt_epoch,
        ) in expected.items():
            response = await client.get(f"/api/v1/research/runs/{run_id}")
            assert response.status_code == 200
            assert response.json() == {
                "runId": str(run_id),
                "threadId": str((await _fetch_run(db_session, run_id)).thread_id),
                "status": status_value,
                "errorCode": error_code,
                "progressStage": progress_stage,
                "attemptEpoch": attempt_epoch,
                "recentEvents": [],
            }

    async def test_returns_recent_events_for_owned_run(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
    ) -> None:
        client, _fake_enqueue = research_client
        thread = await _create_thread(db_session)
        user_message = await _create_message(
            db_session, thread_id=thread.id, seq=1, role="user", content="owned"
        )
        run = await _create_run(
            db_session,
            thread_id=thread.id,
            user_message_id=user_message.id,
            status="running",
            progress_stage="retrieving",
        )
        redis = FakeRunEventsRedis(
            [
                json.dumps(
                    {
                        "type": "external_search.queries_generated",
                        "ts": "2026-07-09T01:00:00+00:00",
                        "task_index": 0,
                        "queries": ["NVIDIA AI"],
                    }
                )
            ]
        )
        app.dependency_overrides[get_redis_client] = lambda: redis

        response = await client.get(f"/api/v1/research/runs/{run.id}")

        assert response.status_code == 200
        assert response.json()["recentEvents"] == [
            {
                "type": "external_search.queries_generated",
                "ts": "2026-07-09T01:00:00Z",
                "taskIndex": 0,
                "queries": ["NVIDIA AI"],
            }
        ]
        assert redis.calls == [(f"agent:run:{run.id}:events", 0, 9)]

    async def test_redis_failure_returns_empty_recent_events_without_500(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
    ) -> None:
        client, _fake_enqueue = research_client
        thread = await _create_thread(db_session)
        user_message = await _create_message(
            db_session, thread_id=thread.id, seq=1, role="user", content="owned"
        )
        run = await _create_run(
            db_session,
            thread_id=thread.id,
            user_message_id=user_message.id,
            status="running",
            progress_stage="retrieving",
        )
        redis = FakeRunEventsRedis(exc=RedisConnectionError("redis down"))
        app.dependency_overrides[get_redis_client] = lambda: redis

        response = await client.get(f"/api/v1/research/runs/{run.id}")

        assert response.status_code == 200
        assert response.json()["recentEvents"] == []
        assert redis.calls == [(f"agent:run:{run.id}:events", 0, 9)]

    async def test_other_users_run_is_404(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
    ) -> None:
        client, _fake_enqueue = research_client
        thread = await _create_thread(db_session, user_id=TEST_ADMIN_ID)
        user_message = await _create_message(
            db_session, thread_id=thread.id, seq=1, role="user", content="other"
        )
        run = await _create_run(
            db_session, thread_id=thread.id, user_message_id=user_message.id
        )
        redis = FakeRunEventsRedis()
        app.dependency_overrides[get_redis_client] = lambda: redis

        response = await client.get(f"/api/v1/research/runs/{run.id}")

        assert response.status_code == 404
        assert redis.calls == []

    async def test_unknown_run_is_404(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
    ) -> None:
        client, _fake_enqueue = research_client

        response = await client.get(
            "/api/v1/research/runs/00000000-0000-4000-a000-000000000099"
        )

        assert response.status_code == 404


def _resolve_ref(schema: dict[str, Any], ref: str) -> dict[str, Any]:
    name = ref.removeprefix("#/components/schemas/")
    return schema["components"]["schemas"][name]


def test_openapi_exposes_async_contract_and_question_shape() -> None:
    app.openapi_schema = None
    schema = app.openapi()
    operation = schema["paths"][_RESPONSES_URL]["post"]
    body_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    request_schema = _resolve_ref(schema, body_schema["$ref"])
    accepted_schema = _resolve_ref(
        schema,
        operation["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
    )

    assert operation["operationId"] == "create_research_response"
    assert request_schema["properties"]["question"]["maxLength"] == 1000
    assert "threadId" in request_schema["properties"]
    assert set(accepted_schema["properties"]) == {"threadId", "runId"}
    assert "404" in operation["responses"]


@pytest.mark.integration
def test_openapi_exposes_flat_typed_daily_limit_without_persistent_error_code() -> None:
    app.openapi_schema = None
    schema = app.openapi()
    operation = schema["paths"][_RESPONSES_URL]["post"]
    quota_response = operation["responses"]["429"]
    quota_schema = _resolve_ref(
        schema,
        quota_response["content"]["application/json"]["schema"]["$ref"],
    )

    assert set(quota_schema["properties"]) == {
        "detail",
        "code",
        "limit",
        "resetAt",
    }
    assert set(quota_schema["required"]) == {
        "detail",
        "code",
        "limit",
        "resetAt",
    }
    assert quota_schema["properties"]["detail"]["const"] == (
        "Daily research request limit exceeded"
    )
    assert quota_schema["properties"]["code"]["const"] == (
        "research_daily_request_limit_exceeded"
    )
    assert quota_schema["properties"]["limit"]["const"] == 10
    assert quota_schema["properties"]["resetAt"]["format"] == "date-time"

    persistent_run_schema = schema["components"]["schemas"]["ResearchRunResponse"]
    assert "research_daily_request_limit_exceeded" not in str(
        persistent_run_schema["properties"]["errorCode"]
    )
    generic_sse_429 = schema["paths"]["/api/v1/research/runs/{run_id}/events"]["get"][
        "responses"
    ]["429"]
    assert "ResearchDailyRequestLimitExceededResponse" not in str(generic_sse_429)


def test_openapi_exposes_thread_ui_contract_and_slim_run_signal() -> None:
    app.openapi_schema = None
    schema = app.openapi()
    paths = schema["paths"]
    assert paths[_THREADS_URL]["get"]["operationId"] == "list_research_threads"
    assert (
        paths[f"{_THREADS_URL}/{{thread_id}}"]["get"]["operationId"]
        == "get_research_thread"
    )
    assert (
        paths[f"{_THREADS_URL}/{{thread_id}}"]["delete"]["operationId"]
        == "delete_research_thread"
    )
    assert (
        paths["/api/v1/research/runs/{run_id}/cancel"]["post"]["operationId"]
        == "cancel_research_run"
    )

    operations_with_not_found = (
        paths[f"{_THREADS_URL}/{{thread_id}}"]["get"],
        paths[f"{_THREADS_URL}/{{thread_id}}"]["delete"],
        paths["/api/v1/research/runs/{run_id}/cancel"]["post"],
        paths["/api/v1/research/runs/{run_id}"]["get"],
    )
    assert all(
        "404" in operation["responses"] for operation in operations_with_not_found
    )

    run_operation = paths["/api/v1/research/runs/{run_id}"]["get"]
    run_schema = _resolve_ref(
        schema,
        run_operation["responses"]["200"]["content"]["application/json"]["schema"][
            "$ref"
        ],
    )
    assert set(run_schema["properties"]) == {
        "runId",
        "threadId",
        "status",
        "errorCode",
        "progressStage",
        "attemptEpoch",
        "recentEvents",
    }
    assert "attemptEpoch" in run_schema["required"]
    assert run_schema["properties"]["attemptEpoch"]["minimum"] == 0
    assert "result" not in run_schema["properties"]
    assert "cancelled" in str(run_schema["properties"]["errorCode"])
    assert "planning" in str(run_schema["properties"]["progressStage"])

    message_run_schema = schema["components"]["schemas"]["ResearchMessageRun"]
    assert set(message_run_schema["properties"]) == {
        "runId",
        "status",
        "errorCode",
        "progressStage",
    }
    assert "attemptEpoch" not in message_run_schema["required"]

    assistant_schema = schema["components"]["schemas"]["ResearchAssistantMessage"]
    assert "[[1]]" in assistant_schema["properties"]["content"]["description"]


def test_openapi_exposes_variant_specific_source_contract() -> None:
    app.openapi_schema = None
    schema = app.openapi()
    internal_schema = schema["components"]["schemas"]["ResearchInternalArticleSource"]
    external_schema = schema["components"]["schemas"]["ResearchExternalUrlSource"]

    assert set(internal_schema["properties"]) == {
        "kind",
        "sourceRef",
        "articleId",
        "title",
        "publishedAt",
    }
    assert "snippet" not in internal_schema["properties"]
    assert "sourceName" not in internal_schema["properties"]
    assert "evidenceClaim" not in internal_schema["properties"]
    assert any(
        branch.get("type") == "null"
        for branch in internal_schema["properties"]["articleId"]["anyOf"]
    )
    assert "evidenceClaim" in external_schema["properties"]
    assert "evidenceClaim" in external_schema["required"]
    assert "snippet" not in external_schema["properties"]
