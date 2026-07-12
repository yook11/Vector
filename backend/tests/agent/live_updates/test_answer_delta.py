"""Direct answer delta coalescer と attempt-local breaker の契約。"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Sequence
from importlib import import_module
from typing import Any, Protocol, cast
from uuid import UUID

import pytest
from logfire.testing import CaptureLogfire
from structlog.testing import capture_logs

from app.agent.live_updates.stream import (
    AgentRunLiveStreamAnswerDeltaEvent,
    AgentRunLiveStreamAnswerResetEvent,
)
from tests.logfire._metric_helpers import collected_metrics

RUN_ID = UUID("00000000-0000-4000-a000-000000000011")
ATTEMPT_EPOCH = 7
BREAKER_METRIC = "vector.agent.answer_delta.breaker_open"


class _AnswerDeltaReporter(Protocol):
    async def append(self, *, generation: int, text: str) -> None: ...

    async def reset(self, *, generation: int) -> None: ...

    async def finish(self, *, generation: int) -> None: ...

    async def abort(self, *, generation: int) -> None: ...


class ManualSleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []
        self.cancelled = 0
        self.active = 0
        self._waiters: list[asyncio.Event] = []
        self._scheduled = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        release = asyncio.Event()
        self.calls.append(seconds)
        self._waiters.append(release)
        self.active += 1
        self._scheduled.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1

    async def wait_until_scheduled(self, expected_count: int) -> None:
        while len(self.calls) < expected_count:
            await self._scheduled.wait()
            self._scheduled.clear()

    def release(self, index: int = 0) -> None:
        self._waiters[index].set()


class ScriptedPublisher:
    def __init__(
        self,
        outcomes: Sequence[str | None | BaseException] = (),
    ) -> None:
        self._outcomes = deque(outcomes)
        self.events: list[
            AgentRunLiveStreamAnswerDeltaEvent | AgentRunLiveStreamAnswerResetEvent
        ] = []
        self.published = asyncio.Event()

    async def publish(self, event: object) -> str | None:
        assert isinstance(
            event,
            AgentRunLiveStreamAnswerDeltaEvent | AgentRunLiveStreamAnswerResetEvent,
        )
        self.events.append(event)
        self.published.set()
        outcome: str | None | BaseException = "1-0"
        if self._outcomes:
            outcome = self._outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class BlockingPublisher(ScriptedPublisher):
    def __init__(self) -> None:
        super().__init__()
        self.release_publish = asyncio.Event()

    async def publish(self, event: object) -> str | None:
        assert isinstance(event, AgentRunLiveStreamAnswerDeltaEvent)
        self.events.append(event)
        self.published.set()
        await self.release_publish.wait()
        return "1-0"


def _new_reporter(
    publisher: ScriptedPublisher,
    sleeper: ManualSleeper,
) -> _AnswerDeltaReporter:
    try:
        module = import_module("app.agent.live_updates.answer_delta")
    except ModuleNotFoundError as exc:
        if exc.name != "app.agent.live_updates.answer_delta":
            raise
        pytest.fail("Direct answer delta reporter が未実装です", pytrace=False)

    reporter_type = getattr(module, "AgentRunLiveAnswerDeltaReporter", None)
    assert reporter_type is not None, "AgentRunLiveAnswerDeltaReporter が未実装です"
    return cast(
        "_AnswerDeltaReporter",
        reporter_type(
            publisher,
            run_id=RUN_ID,
            attempt_epoch=ATTEMPT_EPOCH,
            sleep=sleeper.sleep,
        ),
    )


async def _next_event_loop_turn() -> None:
    loop = asyncio.get_running_loop()
    resumed = loop.create_future()
    loop.call_soon(resumed.set_result, None)
    await resumed


def _texts(publisher: ScriptedPublisher) -> list[str]:
    return [
        event.text
        for event in publisher.events
        if isinstance(event, AgentRunLiveStreamAnswerDeltaEvent)
    ]


def _metric_points(
    capfire: CaptureLogfire,
) -> list[dict[str, Any]]:
    metric = next(
        (item for item in collected_metrics(capfire) if item["name"] == BREAKER_METRIC),
        None,
    )
    if metric is None:
        return []
    return list(metric["data"]["data_points"])


@pytest.mark.asyncio
async def test_first_pending_fragment_flushes_after_independent_250ms_timer() -> None:
    publisher = ScriptedPublisher()
    sleeper = ManualSleeper()
    reporter = _new_reporter(publisher, sleeper)

    await reporter.append(generation=2, text="最初")
    await sleeper.wait_until_scheduled(1)

    assert sleeper.calls == [0.25]
    assert publisher.events == []

    sleeper.release()
    await publisher.published.wait()

    assert publisher.events == [
        AgentRunLiveStreamAnswerDeltaEvent(generation=2, text="最初")
    ]
    await reporter.finish(generation=2)
    assert sleeper.active == 0


@pytest.mark.asyncio
async def test_fragments_inside_one_window_are_coalesced_in_order() -> None:
    publisher = ScriptedPublisher()
    sleeper = ManualSleeper()
    reporter = _new_reporter(publisher, sleeper)

    await reporter.append(generation=3, text="A")
    await sleeper.wait_until_scheduled(1)
    await reporter.append(generation=3, text="B")
    await reporter.append(generation=3, text="C")
    await reporter.finish(generation=3)
    await reporter.finish(generation=3)

    assert publisher.events == [
        AgentRunLiveStreamAnswerDeltaEvent(generation=3, text="ABC")
    ]
    assert sleeper.calls == [0.25]
    assert sleeper.cancelled == 1
    assert sleeper.active == 0


@pytest.mark.parametrize(
    ("length", "expected_lengths"),
    [
        (512, [512]),
        (513, [512, 1]),
        (1025, [512, 512, 1]),
    ],
)
@pytest.mark.asyncio
async def test_size_flush_uses_python_code_points_and_never_exceeds_512(
    length: int,
    expected_lengths: list[int],
) -> None:
    publisher = ScriptedPublisher()
    sleeper = ManualSleeper()
    reporter = _new_reporter(publisher, sleeper)
    original = "界" * length

    await reporter.append(generation=4, text=original)
    await reporter.finish(generation=4)

    assert [len(text) for text in _texts(publisher)] == expected_lengths
    assert "".join(_texts(publisher)) == original
    assert {event.generation for event in publisher.events} == {4}
    assert all(event.text for event in publisher.events)
    assert sleeper.active == 0


@pytest.mark.asyncio
async def test_finish_empty_is_noop_and_abort_drops_pending_timer() -> None:
    empty_publisher = ScriptedPublisher()
    empty_sleeper = ManualSleeper()
    empty_reporter = _new_reporter(empty_publisher, empty_sleeper)

    await empty_reporter.append(generation=1, text="")
    assert empty_publisher.events == []
    assert empty_sleeper.calls == []

    await empty_reporter.finish(generation=1)
    await empty_reporter.finish(generation=1)

    assert empty_publisher.events == []
    assert empty_sleeper.calls == []

    publisher = ScriptedPublisher()
    sleeper = ManualSleeper()
    reporter = _new_reporter(publisher, sleeper)
    await reporter.append(generation=1, text="捨てる本文")
    await sleeper.wait_until_scheduled(1)

    await reporter.abort(generation=1)

    assert publisher.events == []
    assert sleeper.cancelled == 1
    assert sleeper.active == 0


@pytest.mark.asyncio
async def test_abort_generation_allows_next_generation_without_old_text() -> None:
    publisher = ScriptedPublisher()
    sleeper = ManualSleeper()
    reporter = _new_reporter(publisher, sleeper)

    await reporter.append(generation=1, text="破棄")
    await sleeper.wait_until_scheduled(1)
    await reporter.abort(generation=1)
    await reporter.append(generation=2, text="採用")
    await reporter.finish(generation=2)

    assert publisher.events == [
        AgentRunLiveStreamAnswerDeltaEvent(generation=2, text="採用")
    ]
    assert sleeper.active == 0


@pytest.mark.asyncio
async def test_timer_and_finish_race_publishes_each_character_once() -> None:
    publisher = BlockingPublisher()
    sleeper = ManualSleeper()
    reporter = _new_reporter(publisher, sleeper)
    await reporter.append(generation=1, text="競合しても一度")
    await sleeper.wait_until_scheduled(1)

    sleeper.release()
    await publisher.published.wait()
    finish_task = asyncio.create_task(reporter.finish(generation=1))
    await _next_event_loop_turn()
    publisher.release_publish.set()
    await finish_task

    assert _texts(publisher) == ["競合しても一度"]
    assert sleeper.active == 0


@pytest.mark.asyncio
async def test_timer_size_and_finish_race_has_no_loss_duplication_or_reordering() -> (
    None
):
    publisher = BlockingPublisher()
    sleeper = ManualSleeper()
    reporter = _new_reporter(publisher, sleeper)
    await reporter.append(generation=6, text="A")
    await sleeper.wait_until_scheduled(1)

    sleeper.release()
    await publisher.published.wait()
    size_append = asyncio.create_task(reporter.append(generation=6, text="B" * 512))
    await _next_event_loop_turn()
    finish = asyncio.create_task(reporter.finish(generation=6))
    await _next_event_loop_turn()
    publisher.release_publish.set()
    await asyncio.gather(size_append, finish)

    assert "".join(_texts(publisher)) == "A" + ("B" * 512)
    assert all(1 <= len(text) <= 512 for text in _texts(publisher))
    assert {event.generation for event in publisher.events} == {6}
    assert sleeper.active == 0


@pytest.mark.parametrize(
    "outcome",
    [None, RuntimeError("RESET_PUBLISH_SECRET")],
    ids=["unconfirmed", "exception"],
)
@pytest.mark.asyncio
async def test_reset_publishes_immediately_without_timer_or_lazy_retry(
    outcome: str | None | BaseException,
) -> None:
    publisher = ScriptedPublisher([outcome, "must-not-be-used"])
    sleeper = ManualSleeper()
    reporter = _new_reporter(publisher, sleeper)

    await reporter.reset(generation=2)

    assert publisher.events == [AgentRunLiveStreamAnswerResetEvent(generation=2)]
    assert sleeper.calls == []
    assert sleeper.active == 0


@pytest.mark.asyncio
async def test_reset_without_visible_delta_preserves_explicit_generation() -> None:
    publisher = ScriptedPublisher(["1-0", "2-0"])
    sleeper = ManualSleeper()
    reporter = _new_reporter(publisher, sleeper)

    await reporter.reset(generation=8)
    await reporter.reset(generation=8)

    assert publisher.events == [
        AgentRunLiveStreamAnswerResetEvent(generation=8),
        AgentRunLiveStreamAnswerResetEvent(generation=8),
    ]
    assert sleeper.calls == []


@pytest.mark.asyncio
async def test_reset_and_delta_failures_share_one_breaker(
    capfire: CaptureLogfire,
) -> None:
    publisher = ScriptedPublisher(
        [None, RuntimeError("SHARED_BREAKER_SECRET"), None, "must-not-be-used"]
    )
    reporter = _new_reporter(publisher, ManualSleeper())
    secret_text = "RESET_DELTA_PAYLOAD_SECRET".ljust(512, "X")

    with capture_logs() as logs:
        await reporter.reset(generation=4)
        await reporter.append(generation=4, text=secret_text)
        await reporter.reset(generation=4)
        await reporter.append(generation=4, text="blocked".ljust(512, "X"))
        await reporter.reset(generation=5)

    assert publisher.events == [
        AgentRunLiveStreamAnswerResetEvent(generation=4),
        AgentRunLiveStreamAnswerDeltaEvent(generation=4, text=secret_text),
        AgentRunLiveStreamAnswerResetEvent(generation=4),
    ]
    breaker_logs = [
        entry
        for entry in logs
        if entry.get("event") == "agent_run_answer_delta_breaker_open"
    ]
    assert len(breaker_logs) == 1
    assert breaker_logs[0]["generation"] == 4
    serialized_logs = repr(logs)
    assert secret_text not in serialized_logs
    assert "SHARED_BREAKER_SECRET" not in serialized_logs
    points = _metric_points(capfire)
    assert len(points) == 1
    assert points[0]["value"] == 1
    assert points[0].get("attributes", {}) == {"reason": "consecutive_publish_failures"}


@pytest.mark.asyncio
async def test_reset_success_resets_delta_failure_count() -> None:
    publisher = ScriptedPublisher([None, None, "3-0", None, None, "6-0"])
    reporter = _new_reporter(publisher, ManualSleeper())

    await reporter.append(generation=1, text="A" * 512)
    await reporter.append(generation=1, text="B" * 512)
    await reporter.reset(generation=1)
    await reporter.append(generation=1, text="C" * 512)
    await reporter.append(generation=1, text="D" * 512)
    await reporter.reset(generation=1)

    assert len(publisher.events) == 6
    assert publisher.events[2] == AgentRunLiveStreamAnswerResetEvent(generation=1)
    assert publisher.events[5] == AgentRunLiveStreamAnswerResetEvent(generation=1)


@pytest.mark.asyncio
async def test_delta_success_resets_reset_failure_count() -> None:
    publisher = ScriptedPublisher(
        [None, RuntimeError("RESET_FAILURE_SECRET"), "3-0", None, None, "6-0"]
    )
    reporter = _new_reporter(publisher, ManualSleeper())

    await reporter.reset(generation=1)
    await reporter.reset(generation=1)
    await reporter.append(generation=1, text="A" * 512)
    await reporter.reset(generation=1)
    await reporter.reset(generation=1)
    await reporter.append(generation=1, text="B" * 512)

    assert len(publisher.events) == 6
    assert publisher.events[2] == AgentRunLiveStreamAnswerDeltaEvent(
        generation=1,
        text="A" * 512,
    )
    assert publisher.events[5] == AgentRunLiveStreamAnswerDeltaEvent(
        generation=1,
        text="B" * 512,
    )


@pytest.mark.asyncio
async def test_abort_cleans_pending_buffer_and_timer_after_reset_opens_breaker() -> (
    None
):
    publisher = ScriptedPublisher([None, None, None])
    sleeper = ManualSleeper()
    reporter = _new_reporter(publisher, sleeper)
    await reporter.append(generation=1, text="破棄する保留本文")
    await sleeper.wait_until_scheduled(1)

    for _ in range(3):
        await reporter.reset(generation=2)
    await reporter.abort(generation=1)

    assert publisher.events == [
        AgentRunLiveStreamAnswerResetEvent(generation=2),
        AgentRunLiveStreamAnswerResetEvent(generation=2),
        AgentRunLiveStreamAnswerResetEvent(generation=2),
    ]
    assert sleeper.cancelled == 1
    assert sleeper.active == 0


@pytest.mark.asyncio
async def test_three_consecutive_unconfirmed_publishes_open_breaker() -> None:
    publisher = ScriptedPublisher(
        [None, RuntimeError("PUBLISHER_SECRET"), None, "must-not-be-used"]
    )
    sleeper = ManualSleeper()
    reporter = _new_reporter(publisher, sleeper)

    attempts = [
        (1, "A" * 512),
        (1, "B" * 512),
        (1, "C" * 513),
        (2, "D" * 512),
    ]
    for generation, text in attempts:
        await reporter.append(generation=generation, text=text)
    await reporter.finish(generation=1)
    await _next_event_loop_turn()

    assert len(publisher.events) == 3
    assert _texts(publisher) == ["A" * 512, "B" * 512, "C" * 512]
    assert sleeper.active == 0


@pytest.mark.asyncio
async def test_success_resets_consecutive_failure_count() -> None:
    publisher = ScriptedPublisher([None, None, "3-0", None, None, "6-0"])
    sleeper = ManualSleeper()
    reporter = _new_reporter(publisher, sleeper)

    for marker in "ABCDEF":
        await reporter.append(generation=1, text=marker * 512)
    await reporter.finish(generation=1)

    assert len(publisher.events) == 6
    assert _texts(publisher) == [marker * 512 for marker in "ABCDEF"]


@pytest.mark.asyncio
async def test_new_reporter_starts_with_closed_breaker() -> None:
    failed_publisher = ScriptedPublisher([None, None, None])
    failed_reporter = _new_reporter(failed_publisher, ManualSleeper())
    for marker in "ABC":
        await failed_reporter.append(generation=1, text=marker * 512)

    healthy_publisher = ScriptedPublisher(["1-0"])
    healthy_sleeper = ManualSleeper()
    healthy_reporter = _new_reporter(healthy_publisher, healthy_sleeper)
    await healthy_reporter.append(generation=1, text="新attempt")
    await healthy_reporter.finish(generation=1)

    assert _texts(healthy_publisher) == ["新attempt"]


@pytest.mark.asyncio
async def test_breaker_open_is_observed_once_without_payload_or_dynamic_metric_labels(
    capfire: CaptureLogfire,
) -> None:
    publisher = ScriptedPublisher(
        [None, RuntimeError("PUBLISHER_EXCEPTION_SECRET"), None]
    )
    reporter = _new_reporter(publisher, ManualSleeper())
    secret_text = "ANSWER_TEXT_SECRET".ljust(512, "X")
    user_id = "USER_ID_SECRET"

    with capture_logs() as logs:
        for _ in range(4):
            await reporter.append(generation=5, text=secret_text)
        await reporter.finish(generation=5)

    breaker_logs = [
        entry
        for entry in logs
        if entry.get("event") == "agent_run_answer_delta_breaker_open"
    ]
    assert len(breaker_logs) == 1
    assert breaker_logs[0]["run_id"] == str(RUN_ID)
    assert breaker_logs[0]["attempt_epoch"] == ATTEMPT_EPOCH
    assert breaker_logs[0]["generation"] == 5
    serialized_logs = repr(logs)
    assert secret_text not in serialized_logs
    assert "PUBLISHER_EXCEPTION_SECRET" not in serialized_logs
    assert user_id not in serialized_logs

    points = _metric_points(capfire)
    assert len(points) == 1
    assert points[0]["value"] == 1
    assert points[0].get("attributes", {}) == {"reason": "consecutive_publish_failures"}
