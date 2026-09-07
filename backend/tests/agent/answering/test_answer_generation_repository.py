"""回答生成 repository の開始・再生成許可の呼び出し契約。"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

import pytest

from app.agent.running import answer_generation as repository_module
from app.agent.running.answer_generation import (
    AgentAnswerGenerationRepository,
)
from app.agent.runs.execution import Continue

RUN_ID = UUID("019bd239-1ed4-7fbb-a336-04fe3c197652")
ATTEMPT_EPOCH = 3


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
    )

    await repository.start_answer_generation()
    await repository.start_answer_generation()
    await repository.authorize_answer_regeneration()
    await repository.authorize_answer_regeneration()

    assert start_calls == 2
    assert authorize_calls == 2
