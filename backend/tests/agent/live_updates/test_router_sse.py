"""FastAPI SSE endpoint contract tests."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
import redis.asyncio as aioredis
from httpx import ASGITransport, AsyncClient
from starlette.datastructures import State
from starlette.requests import Request

import app.agent.router as router_module
from app.agent.live_updates.sse import AgentRunSseCapacity
from app.agent.live_updates.stream import (
    AgentRunLiveStreamAnswerDeltaEvent,
    AgentRunLiveStreamEntry,
    AgentRunLiveStreamPublisher,
    AgentRunLiveStreamReadResult,
    AgentRunLiveStreamReadStatus,
    AgentRunLiveStreamTerminalEvent,
    agent_run_live_stream_key,
)
from app.agent.runs.contracts import OwnedAgentRunLiveContext
from app.agent.runs.types import AgentRunStatus
from app.config import settings
from app.dependencies import get_redis_client
from app.main import app

RUN_ID = UUID("00000000-0000-4000-a000-000000000011")
URL = f"/api/v1/research/runs/{RUN_ID}/events"


class FakeRedis:
    def __init__(self) -> None:
        self.exists_calls = 0

    async def exists(self, _key: str) -> int:
        self.exists_calls += 1
        return 0


class FakeReader:
    def __init__(self, *results: AgentRunLiveStreamReadResult) -> None:
        self.results = deque(results)
        self.calls: list[tuple[UUID, int, str | None]] = []

    async def read_after(
        self,
        run_id: UUID,
        attempt_epoch: int,
        cursor: str | None,
    ) -> AgentRunLiveStreamReadResult:
        self.calls.append((run_id, attempt_epoch, cursor))
        if self.results:
            return self.results.popleft()
        return AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.UNAVAILABLE
        )


@pytest.fixture
async def sse_client(
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[tuple[AsyncClient, FakeRedis]]:
    redis = FakeRedis()
    app.dependency_overrides[get_redis_client] = lambda: redis
    app.dependency_overrides[router_module.get_agent_run_sse_capacity] = lambda: (
        AgentRunSseCapacity()
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=auth_headers,
    ) as client:
        yield client, redis
    app.dependency_overrides.clear()


def _context(status: AgentRunStatus, *, epoch: int) -> OwnedAgentRunLiveContext:
    return OwnedAgentRunLiveContext(
        run_id=RUN_ID,
        status=status,
        attempt_epoch=epoch,
        error_code=None,
    )


def test_capacity_provider_is_lazy_and_scoped_to_the_fastapi_app() -> None:
    request = cast(
        Request,
        SimpleNamespace(app=SimpleNamespace(state=State())),
    )

    first = router_module.get_agent_run_sse_capacity(request)
    second = router_module.get_agent_run_sse_capacity(request)

    assert first is second


@pytest.mark.asyncio
async def test_endpoint_rejects_malformed_input_before_owned_context_or_redis(
    sse_client: tuple[AsyncClient, FakeRedis],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, redis = sse_client
    context_calls = 0

    async def context(**_kwargs: object) -> None:
        nonlocal context_calls
        context_calls += 1

    monkeypatch.setattr(router_module, "read_agent_run_live_context", context)

    malformed_run = await client.get("/api/v1/research/runs/not-a-uuid/events")
    malformed_cursor = await client.get(URL, headers={"Last-Event-ID": "1-0\rjunk"})

    assert malformed_run.status_code == 400
    assert malformed_cursor.status_code == 400
    assert context_calls == 0
    assert redis.exists_calls == 0


@pytest.mark.asyncio
async def test_endpoint_hides_missing_or_foreign_run_before_redis(
    sse_client: tuple[AsyncClient, FakeRedis],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, redis = sse_client

    async def context(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(router_module, "read_agent_run_live_context", context)

    response = await client.get(URL)

    assert response.status_code == 404
    assert redis.exists_calls == 0


@pytest.mark.asyncio
async def test_endpoint_returns_204_for_terminal_run_without_redis(
    sse_client: tuple[AsyncClient, FakeRedis],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, redis = sse_client

    async def context(**_kwargs: object) -> OwnedAgentRunLiveContext:
        return _context(AgentRunStatus.COMPLETED, epoch=1)

    monkeypatch.setattr(router_module, "read_agent_run_live_context", context)

    response = await client.get(URL)

    assert response.status_code == 204
    assert response.content == b""
    assert redis.exists_calls == 0


@pytest.mark.asyncio
async def test_endpoint_returns_204_for_policy_blocked_run_without_redis(
    sse_client: tuple[AsyncClient, FakeRedis],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, redis = sse_client
    policy_blocked = getattr(AgentRunStatus, "POLICY_BLOCKED", None)
    assert policy_blocked is not None, "policy_blocked must be a terminal run status"

    async def context(**_kwargs: object) -> OwnedAgentRunLiveContext:
        return _context(policy_blocked, epoch=1)

    monkeypatch.setattr(router_module, "read_agent_run_live_context", context)

    response = await client.get(URL)

    assert response.status_code == 204
    assert redis.exists_calls == 0


@pytest.mark.asyncio
async def test_queued_missing_stream_starts_200_then_closes_on_db_terminal(
    sse_client: tuple[AsyncClient, FakeRedis],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, redis = sse_client
    contexts = deque(
        [
            _context(AgentRunStatus.QUEUED, epoch=0),
            _context(AgentRunStatus.FAILED, epoch=0),
        ]
    )

    async def context(**_kwargs: object) -> OwnedAgentRunLiveContext:
        return contexts.popleft()

    monkeypatch.setattr(router_module, "read_agent_run_live_context", context)
    app.dependency_overrides[router_module.get_agent_run_sse_timing] = lambda: (
        router_module.AgentRunSseTiming(
            queued_recheck_interval=0.001,
            queued_wait_limit=0.01,
        )
    )

    response = await client.get(URL)

    assert response.status_code == 200
    assert response.content == b"retry: 1000\n\n"
    assert redis.exists_calls == 1


@pytest.mark.asyncio
async def test_queued_connection_rechecks_policy_blocked_as_a_db_terminal(
    sse_client: tuple[AsyncClient, FakeRedis],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, redis = sse_client
    policy_blocked = getattr(AgentRunStatus, "POLICY_BLOCKED", None)
    assert policy_blocked is not None, "policy_blocked must be a terminal run status"
    contexts = deque(
        [
            _context(AgentRunStatus.QUEUED, epoch=0),
            _context(policy_blocked, epoch=0),
        ]
    )
    context_calls = 0

    async def context(**_kwargs: object) -> OwnedAgentRunLiveContext:
        nonlocal context_calls
        context_calls += 1
        return contexts.popleft()

    monkeypatch.setattr(router_module, "read_agent_run_live_context", context)
    app.dependency_overrides[router_module.get_agent_run_sse_timing] = lambda: (
        router_module.AgentRunSseTiming(
            queued_recheck_interval=0.001,
            queued_wait_limit=0.01,
        )
    )

    response = await client.get(URL)

    assert response.status_code == 200
    assert response.content == b"retry: 1000\n\n"
    assert context_calls == 2
    assert redis.exists_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read_status", "expected_status"),
    [
        (AgentRunLiveStreamReadStatus.CURSOR_TRIMMED, 409),
        (AgentRunLiveStreamReadStatus.UNAVAILABLE, 503),
    ],
)
async def test_endpoint_maps_preflight_failure_before_starting_sse(
    sse_client: tuple[AsyncClient, FakeRedis],
    monkeypatch: pytest.MonkeyPatch,
    read_status: AgentRunLiveStreamReadStatus,
    expected_status: int,
) -> None:
    client, _redis = sse_client

    async def context(**_kwargs: object) -> OwnedAgentRunLiveContext:
        return _context(AgentRunStatus.RUNNING, epoch=2)

    reader = FakeReader(AgentRunLiveStreamReadResult(status=read_status))
    monkeypatch.setattr(router_module, "read_agent_run_live_context", context)
    monkeypatch.setattr(
        router_module,
        "AgentRunLiveStreamReader",
        lambda _redis: reader,
    )

    response = await client.get(URL, headers={"Last-Event-ID": "9-0"})

    assert response.status_code == expected_status
    if expected_status == 503:
        assert response.headers["retry-after"] == "5"
    else:
        assert "retry-after" not in response.headers
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_endpoint_streams_retry_events_and_terminal_with_safe_headers(
    sse_client: tuple[AsyncClient, FakeRedis],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _redis = sse_client

    async def context(**_kwargs: object) -> OwnedAgentRunLiveContext:
        return _context(AgentRunStatus.RUNNING, epoch=2)

    reader = FakeReader(
        AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.EVENTS,
            events=(
                AgentRunLiveStreamEntry(
                    stream_id="10-0",
                    attempt_epoch=2,
                    event=AgentRunLiveStreamAnswerDeltaEvent(
                        generation=1,
                        text="draft",
                    ),
                ),
                AgentRunLiveStreamEntry(
                    stream_id="11-0",
                    attempt_epoch=2,
                    event=AgentRunLiveStreamTerminalEvent(status="completed"),
                ),
            ),
            next_cursor="11-0",
        )
    )
    monkeypatch.setattr(router_module, "read_agent_run_live_context", context)
    monkeypatch.setattr(
        router_module,
        "AgentRunLiveStreamReader",
        lambda _redis: reader,
    )

    response = await client.get(URL)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["cache-control"] == "no-store, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.text.startswith("retry: 1000\n\nid: 10-0\n")
    assert "event: answer.delta" in response.text
    assert response.text.endswith('data: {"attemptEpoch":2,"status":"completed"}\n\n')


@pytest.mark.asyncio
async def test_process_capacity_rejection_precedes_owned_context_read(
    sse_client: tuple[AsyncClient, FakeRedis],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, redis = sse_client
    capacity = AgentRunSseCapacity(process_limit=1)
    assert await capacity.try_acquire_process() is not None
    app.dependency_overrides[router_module.get_agent_run_sse_capacity] = lambda: (
        capacity
    )
    context_calls = 0

    async def context(**_kwargs: object) -> None:
        nonlocal context_calls
        context_calls += 1

    monkeypatch.setattr(router_module, "read_agent_run_live_context", context)

    response = await client.get(URL)

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert context_calls == 0
    assert redis.exists_calls == 0


@pytest.mark.asyncio
async def test_endpoint_requires_bff_user_jwt_before_capacity_or_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    app.dependency_overrides[get_redis_client] = lambda: redis
    capacity = AgentRunSseCapacity(process_limit=1)
    app.dependency_overrides[router_module.get_agent_run_sse_capacity] = lambda: (
        capacity
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(URL)
    app.dependency_overrides.clear()

    assert response.status_code == 401
    assert await capacity.try_acquire_process() is not None
    assert redis.exists_calls == 0


@pytest.mark.asyncio
async def test_full_middleware_stack_propagates_disconnect_and_releases_slot(
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    reader = FakeReader(
        AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.EMPTY,
            next_cursor=None,
        )
    )
    capacity = AgentRunSseCapacity(process_limit=1)

    async def context(**_kwargs: object) -> OwnedAgentRunLiveContext:
        return _context(AgentRunStatus.RUNNING, epoch=2)

    monkeypatch.setattr(router_module, "read_agent_run_live_context", context)
    app.dependency_overrides[router_module.get_agent_run_sse_capacity] = lambda: (
        capacity
    )
    monkeypatch.setattr(
        router_module,
        "AgentRunLiveStreamReader",
        lambda _redis: reader,
    )
    app.dependency_overrides[get_redis_client] = lambda: redis
    response_started = asyncio.Event()
    request_sent = False

    async def receive() -> dict[str, object]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await response_started.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.start":
            response_started.set()

    headers = [
        (key.lower().encode(), value.encode()) for key, value in auth_headers.items()
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/api/v1/research/runs/{RUN_ID}/events",
        "raw_path": f"/api/v1/research/runs/{RUN_ID}/events".encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }
    try:
        await asyncio.wait_for(app(scope, receive, send), timeout=2)
    finally:
        app.dependency_overrides.clear()

    replacement = await capacity.try_acquire_process()
    assert replacement is not None
    await replacement.release()


def test_openapi_exposes_sse_response_contract() -> None:
    app.openapi_schema = None

    operation = app.openapi()["paths"]["/api/v1/research/runs/{run_id}/events"]["get"]

    assert operation["operationId"] == "stream_research_run_events"
    assert set(operation["responses"]) >= {
        "200",
        "204",
        "400",
        "401",
        "404",
        "409",
        "429",
        "503",
    }
    assert "text/event-stream" in operation["responses"]["200"]["content"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_real_redis_events_flow_through_fastapi_sse_with_cursor(
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    run_id = uuid4()

    async def context(**_kwargs: object) -> OwnedAgentRunLiveContext:
        return OwnedAgentRunLiveContext(
            run_id=run_id,
            status=AgentRunStatus.RUNNING,
            attempt_epoch=2,
            error_code=None,
        )

    monkeypatch.setattr(router_module, "read_agent_run_live_context", context)
    app.dependency_overrides[router_module.get_agent_run_sse_capacity] = lambda: (
        AgentRunSseCapacity()
    )
    app.dependency_overrides[get_redis_client] = lambda: redis
    try:
        publisher = AgentRunLiveStreamPublisher(redis, run_id, 2)
        marker_id = await publisher.begin_attempt()
        delta_id = await publisher.publish(
            AgentRunLiveStreamAnswerDeltaEvent(generation=1, text="safe draft")
        )
        terminal_id = await publisher.publish(
            AgentRunLiveStreamTerminalEvent(status="completed")
        )
        assert marker_id is not None
        assert delta_id is not None
        assert terminal_id is not None

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers=auth_headers,
        ) as client:
            response = await client.get(
                f"/api/v1/research/runs/{run_id}/events",
                headers={"Last-Event-ID": marker_id},
            )

        assert response.status_code == 200
        assert f"id: {marker_id}" not in response.text
        assert f"id: {delta_id}" in response.text
        assert f"id: {terminal_id}" in response.text
        assert response.text.index(f"id: {delta_id}") < response.text.index(
            f"id: {terminal_id}"
        )
    finally:
        app.dependency_overrides.clear()
        await redis.delete(agent_run_live_stream_key(run_id))
        await redis.aclose()
