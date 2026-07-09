"""Research async run API contract tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import app.agent.router as research_router_module
from app.config import settings
from app.main import app
from app.models.agent_message import AgentMessage, AgentMessageSource
from app.models.agent_run import AgentRun
from app.models.agent_thread import AgentThread
from tests.conftest import TEST_ADMIN_ID, TEST_USER_ID

_RESPONSES_URL = "/api/v1/research/responses"


class FakeEnqueue:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.calls: list[UUID] = []

    async def __call__(self, run_id: UUID) -> None:
        self.calls.append(run_id)
        if self.exc is not None:
            raise self.exc


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
    app.dependency_overrides[research_router_module.get_agent_history_session] = (
        override_history_session
    )
    monkeypatch.setattr(research_router_module, "enqueue_agent_run", fake_enqueue)
    async with AsyncClient(
        transport=ASGITransport(app=app),
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

    app.dependency_overrides[research_router_module.get_agent_history_session] = (
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
) -> AgentRun:
    run = AgentRun(
        thread_id=thread_id,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        status=status,
        error_code=error_code,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


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
        app.dependency_overrides[research_router_module.get_agent_history_session] = (
            override_history_session
        )
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

        app.dependency_overrides[research_router_module.get_agent_history_session] = (
            override_history_session
        )
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

    async def test_key_missing_fails_fast_without_persisting_run(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, fake_enqueue = research_client
        monkeypatch.setattr(settings, "deepseek_api_key", SecretStr(""))

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


@pytest.mark.asyncio
class TestGetResearchRun:
    async def test_returns_queued_running_failed_and_completed_from_rows(
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
        )

        assert (await client.get(f"/api/v1/research/runs/{queued_run.id}")).json()[
            "status"
        ] == "queued"
        assert (await client.get(f"/api/v1/research/runs/{running_run.id}")).json()[
            "status"
        ] == "running"
        failed_response = await client.get(f"/api/v1/research/runs/{failed_run.id}")
        assert failed_response.json()["errorCode"] == "generation_unavailable"

    async def test_completed_result_allows_null_internal_article_id(
        self,
        research_client: tuple[AsyncClient, FakeEnqueue],
        db_session: AsyncSession,
    ) -> None:
        client, _fake_enqueue = research_client
        thread = await _create_thread(db_session)
        user_message = await _create_message(
            db_session, thread_id=thread.id, seq=1, role="user", content="answer?"
        )
        assistant_message = await _create_message(
            db_session,
            thread_id=thread.id,
            seq=2,
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
        run = await _create_run(
            db_session,
            thread_id=thread.id,
            user_message_id=user_message.id,
            assistant_message_id=assistant_message.id,
            status="completed",
        )

        response = await client.get(f"/api/v1/research/runs/{run.id}")

        assert response.status_code == 200
        assert response.json() == {
            "runId": str(run.id),
            "threadId": str(thread.id),
            "status": "completed",
            "result": {
                "answer": "回答です。[[1]][[2]]",
                "missingAspects": ["未確認の観点"],
                "sources": [
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
                ],
            },
            "errorCode": None,
        }

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

        response = await client.get(f"/api/v1/research/runs/{run.id}")

        assert response.status_code == 404

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
