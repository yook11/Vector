"""SSE lifecycle and capacity tests for active agent runs."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncGenerator
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from starlette.requests import ClientDisconnect

import app.agent.live_updates.sse as sse_module
from app.agent.contract import ExternalSearchCandidatesFetchedEvent
from app.agent.live_updates.sse import (
    AGENT_RUN_SSE_LEASE_TTL_SECONDS,
    AGENT_RUN_SSE_PROCESS_CONNECTION_LIMIT,
    AGENT_RUN_SSE_RUN_CONNECTION_LIMIT,
    AGENT_RUN_SSE_USER_CONNECTION_LIMIT,
    AgentRunQueuedSseConnection,
    AgentRunSseCapacity,
    AgentRunSseCapacityLease,
    AgentRunSseCapacityRejection,
    AgentRunSseConnection,
    AgentRunSsePreflightFailure,
    AgentRunSseTiming,
    prepare_running_sse_connection,
    serialize_agent_run_sse_entry,
    validate_redis_stream_id,
)
from app.agent.live_updates.sse_response import AgentRunSseStreamingResponse
from app.agent.live_updates.stream import (
    AgentRunLiveStreamActivityEvent,
    AgentRunLiveStreamAnswerDeltaEvent,
    AgentRunLiveStreamAnswerResetEvent,
    AgentRunLiveStreamAttemptStartedEvent,
    AgentRunLiveStreamEntry,
    AgentRunLiveStreamEvent,
    AgentRunLiveStreamReadResult,
    AgentRunLiveStreamReadStatus,
    AgentRunLiveStreamStageEvent,
    AgentRunLiveStreamTerminalEvent,
)
from app.agent.runs.contracts import OwnedAgentRunLiveContext
from app.agent.runs.types import AgentRunStatus

USER_1 = UUID("00000000-0000-4000-a000-000000000001")
RUN_1 = UUID("00000000-0000-4000-a000-000000000011")
RUN_2 = UUID("00000000-0000-4000-a000-000000000012")
RUN_3 = UUID("00000000-0000-4000-a000-000000000013")
RUN_4 = UUID("00000000-0000-4000-a000-000000000014")
RUN_5 = UUID("00000000-0000-4000-a000-000000000015")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


class ScriptedReader:
    def __init__(self, *results: AgentRunLiveStreamReadResult) -> None:
        self.results = deque(results)
        self.calls: list[tuple[int, str | None]] = []

    async def read_after(
        self,
        _run_id: UUID,
        attempt_epoch: int,
        cursor: str | None,
    ) -> AgentRunLiveStreamReadResult:
        self.calls.append((attempt_epoch, cursor))
        if self.results:
            return self.results.popleft()
        return AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.EMPTY,
            next_cursor=cursor,
        )


def _entry(
    stream_id: str,
    *,
    epoch: int = 1,
    terminal: bool = False,
) -> AgentRunLiveStreamEntry:
    event: AgentRunLiveStreamEvent
    if terminal:
        event = AgentRunLiveStreamTerminalEvent(status="completed")
    else:
        event = AgentRunLiveStreamAnswerDeltaEvent(generation=1, text=stream_id)
    return AgentRunLiveStreamEntry(
        stream_id=stream_id,
        attempt_epoch=epoch,
        event=event,
    )


async def _collect(connection: AgentRunSseConnection) -> list[bytes]:
    return [frame async for frame in connection.frames()]


@pytest.mark.parametrize(
    ("event", "expected_type", "expected_data"),
    [
        (
            AgentRunLiveStreamAttemptStartedEvent(),
            "attempt.started",
            {"attemptEpoch": 2},
        ),
        (
            AgentRunLiveStreamStageEvent(stage="retrieving"),
            "stage",
            {"attemptEpoch": 2, "stage": "retrieving"},
        ),
        (
            AgentRunLiveStreamActivityEvent(
                activity=ExternalSearchCandidatesFetchedEvent(
                    task_index=3,
                    candidate_count=12,
                )
            ),
            "activity",
            {
                "attemptEpoch": 2,
                "activity": {
                    "type": "external_search.candidates_fetched",
                    "taskIndex": 3,
                    "candidateCount": 12,
                },
            },
        ),
        (
            AgentRunLiveStreamAnswerDeltaEvent(generation=1, text="draft"),
            "answer.delta",
            {"attemptEpoch": 2, "generation": 1, "text": "draft"},
        ),
        (
            AgentRunLiveStreamAnswerResetEvent(generation=2),
            "answer.reset",
            {"attemptEpoch": 2, "generation": 2},
        ),
        (
            AgentRunLiveStreamTerminalEvent(
                status="failed",
                errorCode="cancelled",
            ),
            "terminal",
            {"attemptEpoch": 2, "status": "failed", "errorCode": "cancelled"},
        ),
    ],
)
def test_serializer_projects_the_six_public_events(
    event: AgentRunLiveStreamEvent,
    expected_type: str,
    expected_data: dict[str, object],
) -> None:
    frame = serialize_agent_run_sse_entry(
        AgentRunLiveStreamEntry(
            stream_id="1710000000000-0",
            attempt_epoch=2,
            event=event,
        )
    )

    assert frame is not None
    lines = frame.decode().splitlines()
    assert lines[0] == "id: 1710000000000-0"
    assert lines[1] == f"event: {expected_type}"
    assert json.loads(lines[2].removeprefix("data: ")) == expected_data
    assert lines[3:] == [""]


def test_serializer_escapes_protocol_text_into_one_json_data_line() -> None:
    text = "first\rsecond\n\r\nid: forged\nevent: terminal\nretry: 0\x00"

    frame = serialize_agent_run_sse_entry(
        AgentRunLiveStreamEntry(
            stream_id="1-0",
            attempt_epoch=1,
            event=AgentRunLiveStreamAnswerDeltaEvent(generation=1, text=text),
        )
    )

    assert frame is not None
    decoded = frame.decode()
    assert decoded.count("\ndata: ") == 1
    assert "\nevent: terminal\n" not in decoded
    assert "\nretry: 0\n" not in decoded
    data_line = decoded.splitlines()[2].removeprefix("data: ")
    assert json.loads(data_line)["text"] == text


@pytest.mark.parametrize(
    "stream_id",
    [
        "1",
        "-1-0",
        "1--1",
        "1-0\r",
        "1-0\n",
        "1-0\x00",
        "18446744073709551616-0",
        "0-18446744073709551616",
    ],
)
def test_stream_id_validation_rejects_noncanonical_or_out_of_range_values(
    stream_id: str,
) -> None:
    with pytest.raises(ValueError, match="Redis Stream ID"):
        validate_redis_stream_id(stream_id)


def test_stream_id_validation_accepts_unsigned_64_bit_boundary() -> None:
    boundary = "18446744073709551615-18446744073709551615"

    assert validate_redis_stream_id(boundary) == boundary


def test_serializer_drops_and_counts_unknown_event_without_exposing_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drops = 0

    def record_drop() -> None:
        nonlocal drops
        drops += 1

    monkeypatch.setattr(
        sse_module,
        "record_agent_run_sse_projection_drop",
        record_drop,
    )
    unknown = cast(
        AgentRunLiveStreamEvent,
        SimpleNamespace(type="future.SECRET_EVENT", payload="SECRET_ANSWER"),
    )

    frame = serialize_agent_run_sse_entry(
        AgentRunLiveStreamEntry(
            stream_id="1-0",
            attempt_epoch=1,
            event=unknown,
        )
    )

    assert frame is None
    assert drops == 1


async def _process_lease(capacity: AgentRunSseCapacity) -> AgentRunSseCapacityLease:
    lease = await capacity.try_acquire_process()
    assert lease is not None
    return lease


def test_sse_capacity_defaults_match_the_process_contract() -> None:
    assert AGENT_RUN_SSE_RUN_CONNECTION_LIMIT == 2
    assert AGENT_RUN_SSE_USER_CONNECTION_LIMIT == 4
    assert AGENT_RUN_SSE_PROCESS_CONNECTION_LIMIT == 50
    assert AGENT_RUN_SSE_LEASE_TTL_SECONDS == 55


@pytest.mark.asyncio
async def test_sse_capacity_allows_two_connections_per_run_and_rejects_third() -> None:
    capacity = AgentRunSseCapacity()
    first = await _process_lease(capacity)
    second = await _process_lease(capacity)
    third = await _process_lease(capacity)

    assert await first.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None
    assert await second.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None
    assert (
        await third.try_acquire_owned(run_id=RUN_1, user_id=USER_1)
        is AgentRunSseCapacityRejection.RUN
    )

    await first.release()
    await second.release()


@pytest.mark.asyncio
async def test_sse_capacity_serializes_concurrent_run_acquisitions() -> None:
    capacity = AgentRunSseCapacity(process_limit=3)
    start = asyncio.Event()

    async def acquire() -> tuple[
        AgentRunSseCapacityLease,
        AgentRunSseCapacityRejection | None,
    ]:
        lease = await _process_lease(capacity)
        await start.wait()
        rejection = await lease.try_acquire_owned(run_id=RUN_1, user_id=USER_1)
        return lease, rejection

    tasks = [asyncio.create_task(acquire()) for _ in range(3)]
    await asyncio.sleep(0)
    start.set()
    outcomes = await asyncio.gather(*tasks)

    assert [rejection for _lease, rejection in outcomes].count(None) == 2
    assert [rejection for _lease, rejection in outcomes].count(
        AgentRunSseCapacityRejection.RUN
    ) == 1
    for lease, rejection in outcomes:
        if rejection is None:
            await lease.release()


@pytest.mark.asyncio
async def test_sse_capacity_allows_four_connections_per_user_across_runs() -> None:
    capacity = AgentRunSseCapacity()
    leases = [await _process_lease(capacity) for _ in range(5)]

    for lease, run_id in zip(leases[:4], (RUN_1, RUN_2, RUN_3, RUN_4), strict=True):
        assert await lease.try_acquire_owned(run_id=run_id, user_id=USER_1) is None
    assert (
        await leases[4].try_acquire_owned(run_id=RUN_5, user_id=USER_1)
        is AgentRunSseCapacityRejection.USER
    )

    for lease in leases[:4]:
        await lease.release()


@pytest.mark.asyncio
async def test_sse_capacity_rejects_the_next_process_connection() -> None:
    capacity = AgentRunSseCapacity(process_limit=2)
    first = await _process_lease(capacity)
    second = await _process_lease(capacity)

    assert await capacity.try_acquire_process() is None

    await first.release()
    recovered = await capacity.try_acquire_process()
    assert recovered is not None
    await recovered.release()
    await second.release()


@pytest.mark.asyncio
async def test_default_process_capacity_accepts_fifty_and_rejects_fifty_first() -> None:
    capacity = AgentRunSseCapacity()
    leases = [await _process_lease(capacity) for _ in range(50)]

    assert await capacity.try_acquire_process() is None

    for lease in leases:
        await lease.release()


@pytest.mark.asyncio
async def test_keyed_rejection_releases_the_reserved_process_slot() -> None:
    capacity = AgentRunSseCapacity(process_limit=2, run_limit=1)
    accepted = await _process_lease(capacity)
    rejected = await _process_lease(capacity)
    assert await accepted.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None

    assert (
        await rejected.try_acquire_owned(run_id=RUN_1, user_id=USER_1)
        is AgentRunSseCapacityRejection.RUN
    )
    replacement = await capacity.try_acquire_process()
    assert replacement is not None

    await replacement.release()
    await accepted.release()


@pytest.mark.asyncio
async def test_capacity_release_is_idempotent_and_exception_safe() -> None:
    capacity = AgentRunSseCapacity(process_limit=1, run_limit=1, user_limit=1)
    lease = await _process_lease(capacity)

    with pytest.raises(RuntimeError, match="stream failed"):
        async with lease:
            assert await lease.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None
            raise RuntimeError("stream failed")

    await lease.release()
    recovered = await _process_lease(capacity)
    assert await recovered.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None
    await recovered.release()


@pytest.mark.asyncio
async def test_new_capacity_allocator_has_no_persistent_lease_state() -> None:
    crashed_process = AgentRunSseCapacity(process_limit=1)
    assert await crashed_process.try_acquire_process() is not None

    restarted_process = AgentRunSseCapacity(process_limit=1)
    lease = await restarted_process.try_acquire_process()

    assert lease is not None
    await lease.release()


@pytest.mark.asyncio
async def test_expired_unreleased_lease_is_pruned_before_capacity_check() -> None:
    clock = FakeClock()
    capacity = AgentRunSseCapacity(
        process_limit=1,
        lease_ttl_seconds=5,
        clock=clock,
    )
    stale = await _process_lease(capacity)
    assert await stale.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None
    clock.now = 5

    replacement = await capacity.try_acquire_process()

    assert replacement is not None
    assert await replacement.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None
    await replacement.release()


@pytest.mark.asyncio
async def test_response_start_failure_releases_lease_before_body_iteration() -> None:
    capacity = AgentRunSseCapacity(process_limit=1)
    lease = await _process_lease(capacity)
    assert await lease.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None
    body_started = False

    async def body() -> AsyncGenerator[bytes]:
        nonlocal body_started
        body_started = True
        yield b"never sent"

    response = AgentRunSseStreamingResponse(
        body(),
        lease=lease,
        media_type="text/event-stream",
    )

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    async def send(_message: dict[str, object]) -> None:
        raise OSError("client disconnected before response start")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/events",
        "raw_path": b"/events",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }

    with pytest.raises(ClientDisconnect):
        await response(scope, receive, send)

    assert body_started is False
    replacement = await capacity.try_acquire_process()
    assert replacement is not None
    await replacement.release()


@pytest.mark.asyncio
async def test_terminal_stops_connection_and_releases_before_eof() -> None:
    capacity = AgentRunSseCapacity(process_limit=1)
    lease = await _process_lease(capacity)
    assert await lease.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None
    reader = ScriptedReader(
        AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.EVENTS,
            events=(_entry("1-0"), _entry("2-0", terminal=True), _entry("3-0")),
            next_cursor="3-0",
        )
    )
    clock = FakeClock()
    prepared = await prepare_running_sse_connection(
        run_id=RUN_1,
        attempt_epoch=1,
        cursor=None,
        reader=reader,
        lease=lease,
        timing=AgentRunSseTiming(),
        clock=clock,
        sleep=clock.sleep,
    )
    assert isinstance(prepared, AgentRunSseConnection)

    frames = await _collect(prepared)

    assert frames[0] == b"retry: 1000\n\n"
    assert [frame.splitlines()[0] for frame in frames[1:]] == [b"id: 1-0", b"id: 2-0"]
    replacement = await capacity.try_acquire_process()
    assert replacement is not None
    await replacement.release()


@pytest.mark.asyncio
async def test_connection_repins_without_consuming_boundary_cursor() -> None:
    capacity = AgentRunSseCapacity()
    lease = await _process_lease(capacity)
    assert await lease.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None
    reader = ScriptedReader(
        AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.EVENTS,
            events=(_entry("1-0"),),
            next_cursor="1-0",
        ),
        AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.ATTEMPT_ADVANCED,
            next_cursor="1-5",
            observed_attempt_epoch=3,
        ),
        AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.EVENTS,
            events=(
                AgentRunLiveStreamEntry(
                    stream_id="2-0",
                    attempt_epoch=3,
                    event=AgentRunLiveStreamAttemptStartedEvent(),
                ),
                _entry("3-0", epoch=3, terminal=True),
            ),
            next_cursor="3-0",
        ),
    )
    clock = FakeClock()
    prepared = await prepare_running_sse_connection(
        run_id=RUN_1,
        attempt_epoch=1,
        cursor=None,
        reader=reader,
        lease=lease,
        timing=AgentRunSseTiming(),
        clock=clock,
        sleep=clock.sleep,
    )
    assert isinstance(prepared, AgentRunSseConnection)

    frames = await _collect(prepared)

    assert reader.calls[:3] == [(1, None), (1, "1-0"), (3, "1-5")]
    assert [frame.splitlines()[0] for frame in frames[1:]] == [
        b"id: 1-0",
        b"id: 2-0",
        b"id: 3-0",
    ]


@pytest.mark.asyncio
async def test_preflight_repin_preserves_advanced_next_cursor() -> None:
    capacity = AgentRunSseCapacity()
    lease = await _process_lease(capacity)
    assert await lease.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None
    reader = ScriptedReader(
        AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.ATTEMPT_ADVANCED,
            next_cursor="2-0",
            observed_attempt_epoch=3,
        ),
        AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.EVENTS,
            events=(_entry("3-0", epoch=3, terminal=True),),
            next_cursor="3-0",
        ),
    )
    clock = FakeClock()

    prepared = await prepare_running_sse_connection(
        run_id=RUN_1,
        attempt_epoch=1,
        cursor="1-0",
        reader=reader,
        lease=lease,
        timing=AgentRunSseTiming(),
        clock=clock,
        sleep=clock.sleep,
    )
    assert isinstance(prepared, AgentRunSseConnection)
    await _collect(prepared)

    assert reader.calls == [(1, "1-0"), (3, "2-0")]


@pytest.mark.asyncio
async def test_preflight_handles_multiple_epoch_advances_with_latest_cursor() -> None:
    capacity = AgentRunSseCapacity()
    lease = await _process_lease(capacity)
    assert await lease.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None
    reader = ScriptedReader(
        AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.ATTEMPT_ADVANCED,
            next_cursor="2-0",
            observed_attempt_epoch=2,
        ),
        AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.ATTEMPT_ADVANCED,
            next_cursor="3-0",
            observed_attempt_epoch=4,
        ),
        AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.EVENTS,
            events=(_entry("4-0", epoch=4, terminal=True),),
            next_cursor="4-0",
        ),
    )

    prepared = await prepare_running_sse_connection(
        run_id=RUN_1,
        attempt_epoch=1,
        cursor="1-0",
        reader=reader,
        lease=lease,
        timing=AgentRunSseTiming(),
    )
    assert isinstance(prepared, AgentRunSseConnection)
    await _collect(prepared)

    assert reader.calls == [(1, "1-0"), (2, "2-0"), (4, "3-0")]


@pytest.mark.asyncio
async def test_repin_attempt_absent_grace_closes_after_two_seconds() -> None:
    capacity = AgentRunSseCapacity()
    lease = await _process_lease(capacity)
    assert await lease.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None
    reader = ScriptedReader(
        AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.EMPTY,
            next_cursor="1-0",
        ),
        AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.ATTEMPT_ADVANCED,
            next_cursor="1-5",
            observed_attempt_epoch=2,
        ),
        *[
            AgentRunLiveStreamReadResult(
                status=AgentRunLiveStreamReadStatus.ATTEMPT_ABSENT,
                next_cursor=f"1-{index + 6}",
            )
            for index in range(5)
        ],
    )
    clock = FakeClock()
    prepared = await prepare_running_sse_connection(
        run_id=RUN_1,
        attempt_epoch=1,
        cursor="1-0",
        reader=reader,
        lease=lease,
        timing=AgentRunSseTiming(),
        clock=clock,
        sleep=clock.sleep,
    )
    assert isinstance(prepared, AgentRunSseConnection)

    assert await _collect(prepared) == [b"retry: 1000\n\n"]
    assert clock.now == pytest.approx(2.5)
    assert reader.calls[-1] == (2, "1-9")


@pytest.mark.asyncio
async def test_midstream_missing_grace_silently_closes_after_two_seconds() -> None:
    capacity = AgentRunSseCapacity()
    lease = await _process_lease(capacity)
    assert await lease.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None
    reader = ScriptedReader(
        AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.EMPTY,
            next_cursor="1-0",
        ),
        *[
            AgentRunLiveStreamReadResult(
                status=AgentRunLiveStreamReadStatus.STREAM_MISSING
            )
            for _ in range(5)
        ],
    )
    clock = FakeClock()
    prepared = await prepare_running_sse_connection(
        run_id=RUN_1,
        attempt_epoch=1,
        cursor="1-0",
        reader=reader,
        lease=lease,
        timing=AgentRunSseTiming(),
        clock=clock,
        sleep=clock.sleep,
    )
    assert isinstance(prepared, AgentRunSseConnection)

    assert await _collect(prepared) == [b"retry: 1000\n\n"]
    assert clock.now == pytest.approx(2.5)


@pytest.mark.asyncio
async def test_follow_zombie_only_batch_advances_cursor_without_closing() -> None:
    capacity = AgentRunSseCapacity()
    lease = await _process_lease(capacity)
    assert await lease.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None
    reader = ScriptedReader(
        AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.EMPTY,
            next_cursor="4-0",
        ),
        AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.ATTEMPT_ABSENT,
            next_cursor="5-0",
        ),
        AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.EVENTS,
            events=(_entry("6-0", terminal=True),),
            next_cursor="6-0",
        ),
    )
    clock = FakeClock()
    prepared = await prepare_running_sse_connection(
        run_id=RUN_1,
        attempt_epoch=1,
        cursor="4-0",
        reader=reader,
        lease=lease,
        timing=AgentRunSseTiming(),
        clock=clock,
        sleep=clock.sleep,
    )
    assert isinstance(prepared, AgentRunSseConnection)

    frames = await _collect(prepared)

    assert reader.calls == [(1, "4-0"), (1, "4-0"), (1, "5-0")]
    assert frames[-1].startswith(b"id: 6-0\n")


@pytest.mark.asyncio
async def test_connection_heartbeat_has_no_id_and_max_age_is_not_extended() -> None:
    capacity = AgentRunSseCapacity()
    lease = await _process_lease(capacity)
    assert await lease.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None
    reader = ScriptedReader(
        AgentRunLiveStreamReadResult(status=AgentRunLiveStreamReadStatus.EMPTY)
    )
    clock = FakeClock()
    timing = replace(
        AgentRunSseTiming(),
        follow_interval=1,
        heartbeat_interval=2,
        connection_max_age=5,
    )
    prepared = await prepare_running_sse_connection(
        run_id=RUN_1,
        attempt_epoch=1,
        cursor=None,
        reader=reader,
        lease=lease,
        timing=timing,
        clock=clock,
        sleep=clock.sleep,
    )
    assert isinstance(prepared, AgentRunSseConnection)

    frames = await _collect(prepared)

    assert frames == [b"retry: 1000\n\n", b": heartbeat\n\n", b": heartbeat\n\n"]
    assert clock.now == pytest.approx(5)


@pytest.mark.asyncio
async def test_midstream_unavailable_silently_closes_and_releases_capacity() -> None:
    capacity = AgentRunSseCapacity(process_limit=1)
    lease = await _process_lease(capacity)
    assert await lease.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None
    reader = ScriptedReader(
        AgentRunLiveStreamReadResult(status=AgentRunLiveStreamReadStatus.EMPTY),
        AgentRunLiveStreamReadResult(status=AgentRunLiveStreamReadStatus.UNAVAILABLE),
    )
    clock = FakeClock()
    prepared = await prepare_running_sse_connection(
        run_id=RUN_1,
        attempt_epoch=1,
        cursor=None,
        reader=reader,
        lease=lease,
        timing=AgentRunSseTiming(),
        clock=clock,
        sleep=clock.sleep,
    )
    assert isinstance(prepared, AgentRunSseConnection)

    assert await _collect(prepared) == [b"retry: 1000\n\n"]
    replacement = await capacity.try_acquire_process()
    assert replacement is not None
    await replacement.release()


@pytest.mark.asyncio
async def test_client_disconnect_cancels_follow_and_releases_capacity() -> None:
    capacity = AgentRunSseCapacity(process_limit=1)
    lease = await _process_lease(capacity)
    assert await lease.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None
    reader = ScriptedReader(
        AgentRunLiveStreamReadResult(status=AgentRunLiveStreamReadStatus.EMPTY)
    )
    clock = FakeClock()

    async def disconnected() -> bool:
        return len(reader.calls) >= 1

    prepared = await prepare_running_sse_connection(
        run_id=RUN_1,
        attempt_epoch=1,
        cursor=None,
        reader=reader,
        lease=lease,
        timing=AgentRunSseTiming(),
        clock=clock,
        sleep=clock.sleep,
        is_disconnected=disconnected,
    )
    assert isinstance(prepared, AgentRunSseConnection)

    assert await _collect(prepared) == [b"retry: 1000\n\n"]
    replacement = await capacity.try_acquire_process()
    assert replacement is not None
    await replacement.release()


@pytest.mark.asyncio
async def test_initial_stream_missing_stops_at_bounded_grace() -> None:
    capacity = AgentRunSseCapacity(process_limit=1)
    lease = await _process_lease(capacity)
    assert await lease.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None
    reader = ScriptedReader(
        *[
            AgentRunLiveStreamReadResult(
                status=AgentRunLiveStreamReadStatus.STREAM_MISSING
            )
            for _ in range(5)
        ]
    )
    clock = FakeClock()

    prepared = await prepare_running_sse_connection(
        run_id=RUN_1,
        attempt_epoch=1,
        cursor=None,
        reader=reader,
        lease=lease,
        timing=AgentRunSseTiming(),
        clock=clock,
        sleep=clock.sleep,
    )

    assert prepared is AgentRunSsePreflightFailure.UNAVAILABLE
    assert clock.now == pytest.approx(2)
    assert len(reader.calls) == 5
    replacement = await capacity.try_acquire_process()
    assert replacement is not None
    await replacement.release()


@pytest.mark.asyncio
async def test_preflight_failure_does_not_count_as_open_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[tuple[str, object]] = []
    monkeypatch.setattr(
        sse_module,
        "record_agent_run_sse_open",
        lambda: recorded.append(("open", None)),
    )
    monkeypatch.setattr(
        sse_module,
        "record_agent_run_sse_close",
        lambda **kwargs: recorded.append(("close", kwargs)),
    )
    capacity = AgentRunSseCapacity()
    lease = await _process_lease(capacity)
    assert await lease.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None

    result = await prepare_running_sse_connection(
        run_id=RUN_1,
        attempt_epoch=1,
        cursor=None,
        reader=ScriptedReader(
            AgentRunLiveStreamReadResult(
                status=AgentRunLiveStreamReadStatus.UNAVAILABLE
            )
        ),
        lease=lease,
        timing=AgentRunSseTiming(),
    )

    assert result is AgentRunSsePreflightFailure.UNAVAILABLE
    assert recorded == []


@pytest.mark.asyncio
async def test_task_cancellation_records_client_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = 0
    close_reasons: list[str] = []

    def record_open() -> None:
        nonlocal opened
        opened += 1

    monkeypatch.setattr(sse_module, "record_agent_run_sse_open", record_open)
    monkeypatch.setattr(
        sse_module,
        "record_agent_run_sse_close",
        lambda **kwargs: close_reasons.append(cast(str, kwargs["reason"])),
    )
    capacity = AgentRunSseCapacity()
    lease = await _process_lease(capacity)
    assert await lease.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None
    never = asyncio.Event()

    async def wait_forever(_seconds: float) -> None:
        await never.wait()

    connection = AgentRunSseConnection(
        run_id=RUN_1,
        attempt_epoch=1,
        cursor=None,
        reader=ScriptedReader(
            AgentRunLiveStreamReadResult(status=AgentRunLiveStreamReadStatus.EMPTY)
        ),
        lease=lease,
        initial_result=AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.EMPTY
        ),
        timing=AgentRunSseTiming(),
        started_at=0,
        clock=lambda: 0,
        sleep=wait_forever,
        is_disconnected=None,
    )
    frames = connection.frames()
    assert await anext(frames) == b"retry: 1000\n\n"
    pending = asyncio.create_task(anext(frames))
    await asyncio.sleep(0)

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert opened == 1
    assert close_reasons == ["client_disconnect"]


@pytest.mark.asyncio
async def test_queued_connection_never_reads_epoch_zero_and_moves_to_running() -> None:
    capacity = AgentRunSseCapacity()
    lease = await _process_lease(capacity)
    assert await lease.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None
    contexts = deque(
        [
            OwnedAgentRunLiveContext(
                run_id=RUN_1,
                status=AgentRunStatus.RUNNING,
                attempt_epoch=2,
                error_code=None,
            )
        ]
    )

    async def load_context() -> OwnedAgentRunLiveContext:
        return contexts.popleft()

    reader = ScriptedReader(
        AgentRunLiveStreamReadResult(
            status=AgentRunLiveStreamReadStatus.EVENTS,
            events=(_entry("8-0", epoch=2, terminal=True),),
            next_cursor="8-0",
        )
    )
    clock = FakeClock()
    connection = AgentRunQueuedSseConnection(
        run_id=RUN_1,
        cursor=None,
        reader=reader,
        lease=lease,
        load_context=load_context,
        timing=AgentRunSseTiming(),
        started_at=clock(),
        clock=clock,
        sleep=clock.sleep,
        is_disconnected=None,
    )

    frames = await _collect(connection)

    assert reader.calls == [(2, None)]
    assert frames[0] == b"retry: 1000\n\n"
    assert frames[1].startswith(b"id: 8-0\n")


@pytest.mark.asyncio
async def test_queued_terminal_transition_closes_without_synthetic_event() -> None:
    capacity = AgentRunSseCapacity(process_limit=1)
    lease = await _process_lease(capacity)
    assert await lease.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None

    async def load_context() -> OwnedAgentRunLiveContext:
        return OwnedAgentRunLiveContext(
            run_id=RUN_1,
            status=AgentRunStatus.FAILED,
            attempt_epoch=0,
            error_code=None,
        )

    reader = ScriptedReader()
    clock = FakeClock()
    connection = AgentRunQueuedSseConnection(
        run_id=RUN_1,
        cursor=None,
        reader=reader,
        lease=lease,
        load_context=load_context,
        timing=AgentRunSseTiming(),
        started_at=clock(),
        clock=clock,
        sleep=clock.sleep,
        is_disconnected=None,
    )

    assert await _collect(connection) == [b"retry: 1000\n\n"]
    assert reader.calls == []
    replacement = await capacity.try_acquire_process()
    assert replacement is not None
    await replacement.release()


@pytest.mark.asyncio
async def test_queued_wait_limit_is_finite_and_does_not_overlap_db_reads() -> None:
    capacity = AgentRunSseCapacity()
    lease = await _process_lease(capacity)
    assert await lease.try_acquire_owned(run_id=RUN_1, user_id=USER_1) is None
    active_reads = 0
    max_active_reads = 0
    read_count = 0

    async def load_context() -> OwnedAgentRunLiveContext:
        nonlocal active_reads, max_active_reads, read_count
        active_reads += 1
        max_active_reads = max(max_active_reads, active_reads)
        read_count += 1
        await asyncio.sleep(0)
        active_reads -= 1
        return OwnedAgentRunLiveContext(
            run_id=RUN_1,
            status=AgentRunStatus.QUEUED,
            attempt_epoch=0,
            error_code=None,
        )

    clock = FakeClock()
    timing = replace(
        AgentRunSseTiming(),
        queued_recheck_interval=2,
        queued_wait_limit=5,
        connection_max_age=20,
    )
    connection = AgentRunQueuedSseConnection(
        run_id=RUN_1,
        cursor=None,
        reader=ScriptedReader(),
        lease=lease,
        load_context=load_context,
        timing=timing,
        started_at=clock(),
        clock=clock,
        sleep=clock.sleep,
        is_disconnected=None,
    )

    assert await _collect(connection) == [b"retry: 1000\n\n"]
    assert clock.now == pytest.approx(5)
    assert read_count == 3
    assert max_active_reads == 1
