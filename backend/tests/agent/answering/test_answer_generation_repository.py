"""回答生成 repository の継続確認キャッシュ契約。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

import pytest

from app.agent.answering import answer_generation_repository as repository_module
from app.agent.answering.answer_generation_repository import (
    ANSWER_GENERATION_CONTINUATION_INTERVAL_SECONDS,
    AgentAnswerGenerationRepository,
)
from app.agent.runs.execution import Continue, Stop, StopReason

RUN_ID = UUID("019bd239-1ed4-7fbb-a336-04fe3c197652")
ATTEMPT_EPOCH = 3


@dataclass
class ManualClock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _NullTransaction:
    async def __aenter__(self) -> _NullTransaction:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeSession:
    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def begin(self) -> _NullTransaction:
        return _NullTransaction()


def _factory() -> Callable[[], FakeSession]:
    return FakeSession


@pytest.mark.asyncio
async def test_continuation_check_reuses_continue_within_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def read_once(*_args: object, **_kwargs: object) -> Continue:
        nonlocal calls
        calls += 1
        return Continue()

    monkeypatch.setattr(
        repository_module,
        "_check_answer_generation_continuation",
        read_once,
    )
    clock = ManualClock()
    repository = AgentAnswerGenerationRepository(
        _factory(),
        RUN_ID,
        ATTEMPT_EPOCH,
        clock=clock,
    )

    first = await repository.check_answer_generation_continuation()
    clock.advance(ANSWER_GENERATION_CONTINUATION_INTERVAL_SECONDS - 0.01)
    second = await repository.check_answer_generation_continuation()

    assert first == Continue()
    assert second == Continue()
    assert calls == 1


@pytest.mark.asyncio
async def test_continuation_check_rereads_after_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def read_continue(*_args: object, **_kwargs: object) -> Continue:
        nonlocal calls
        calls += 1
        return Continue()

    monkeypatch.setattr(
        repository_module,
        "_check_answer_generation_continuation",
        read_continue,
    )
    clock = ManualClock()
    repository = AgentAnswerGenerationRepository(
        _factory(),
        RUN_ID,
        ATTEMPT_EPOCH,
        clock=clock,
    )

    await repository.check_answer_generation_continuation()
    clock.advance(ANSWER_GENERATION_CONTINUATION_INTERVAL_SECONDS)
    await repository.check_answer_generation_continuation()

    assert calls == 2


@pytest.mark.asyncio
async def test_continuation_stop_is_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def read_stop(*_args: object, **_kwargs: object) -> Stop:
        nonlocal calls
        calls += 1
        return Stop(StopReason.NOT_CURRENT)

    monkeypatch.setattr(
        repository_module,
        "_check_answer_generation_continuation",
        read_stop,
    )
    clock = ManualClock()
    repository = AgentAnswerGenerationRepository(
        _factory(),
        RUN_ID,
        ATTEMPT_EPOCH,
        clock=clock,
    )

    first = await repository.check_answer_generation_continuation()
    clock.advance(ANSWER_GENERATION_CONTINUATION_INTERVAL_SECONDS)
    second = await repository.check_answer_generation_continuation()

    assert first == Stop(StopReason.NOT_CURRENT)
    assert second == Stop(StopReason.NOT_CURRENT)
    assert calls == 1


@pytest.mark.asyncio
async def test_start_and_authorize_do_not_use_continuation_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_calls = 0
    authorize_calls = 0

    async def start_once(*_args: object, **_kwargs: object) -> Continue:
        nonlocal start_calls
        start_calls += 1
        return Continue()

    async def authorize_once(*_args: object, **_kwargs: object) -> Continue:
        nonlocal authorize_calls
        authorize_calls += 1
        return Continue()

    monkeypatch.setattr(repository_module, "_start_answer_generation", start_once)
    monkeypatch.setattr(
        repository_module, "_authorize_answer_regeneration", authorize_once
    )
    repository = AgentAnswerGenerationRepository(
        _factory(),
        RUN_ID,
        ATTEMPT_EPOCH,
        clock=ManualClock(),
    )

    await repository.start_answer_generation()
    await repository.start_answer_generation()
    await repository.authorize_answer_regeneration()
    await repository.authorize_answer_regeneration()

    assert start_calls == 2
    assert authorize_calls == 2
