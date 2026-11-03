"""回答生成 repository の開始・再生成許可の呼び出し契約。

前提: DB は使わず、session と AgentRunStateRepository の判定を差し替える。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.agent.running.answer_generation import AgentAnswerGenerationRepository
from app.agent.running.state import AnswerGenerationStarted
from app.agent.running.state_repository import AgentRunStateRepository

RUN_ID = UUID("019bd239-1ed4-7fbb-a336-04fe3c197652")
ATTEMPT_EPOCH = 3
STARTED_AT = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


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
async def test_start_and_authorize_each_call_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """開始と再生成許可は呼ぶたびに AgentRunStateRepository へ問い合わせる。"""
    start_calls = 0
    authorize_calls = 0

    async def start_once(*_args: object, **_kwargs: object) -> AnswerGenerationStarted:
        nonlocal start_calls
        start_calls += 1
        return AnswerGenerationStarted(RUN_ID, STARTED_AT)

    async def authorize_once(*_args: object, **_kwargs: object) -> None:
        nonlocal authorize_calls
        authorize_calls += 1

    monkeypatch.setattr(AgentRunStateRepository, "start_answer_generation", start_once)
    monkeypatch.setattr(
        AgentRunStateRepository, "authorize_answer_regeneration", authorize_once
    )
    repository = AgentAnswerGenerationRepository(
        _factory(),
        RUN_ID,
        ATTEMPT_EPOCH,
    )

    await repository.start_answer_generation()
    await repository.start_answer_generation()
    await repository.authorize_answer_regeneration()
    await repository.authorize_answer_regeneration()

    assert start_calls == 2
    assert authorize_calls == 2
