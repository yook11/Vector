"""回答生成 repository の開始・再生成許可の呼び出し契約。

前提: DB は使わず、AgentRunStateService の操作を差し替える。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.agent.running.answer_generation import AgentAnswerGenerationRepository
from app.agent.running.state import AnswerGenerationStarted, CanContinue
from app.agent.running.state_service import AgentRunStateService

RUN_ID = UUID("019bd239-1ed4-7fbb-a336-04fe3c197652")
ATTEMPT_EPOCH = 3
STARTED_AT = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def _unused_session_factory() -> object:
    raise AssertionError("state service operations are replaced in this test")


def _unused_publisher(_run_id: UUID, _attempt_epoch: int) -> object:
    raise AssertionError("no run is closed in this test")


@pytest.mark.asyncio
async def test_start_and_authorize_each_call_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """開始と再生成許可は呼ぶたびに AgentRunStateService の操作へ問い合わせる。"""
    start_calls = 0
    authorize_calls = 0

    async def start_once(*_args: object, **_kwargs: object) -> AnswerGenerationStarted:
        nonlocal start_calls
        start_calls += 1
        return AnswerGenerationStarted(RUN_ID, STARTED_AT)

    async def authorize_once(*_args: object, **_kwargs: object) -> CanContinue:
        nonlocal authorize_calls
        authorize_calls += 1
        return CanContinue()

    monkeypatch.setattr(AgentRunStateService, "start_answer_generation", start_once)
    monkeypatch.setattr(
        AgentRunStateService, "authorize_answer_regeneration", authorize_once
    )
    repository = AgentAnswerGenerationRepository(
        AgentRunStateService(
            session_factory=_unused_session_factory,  # type: ignore[arg-type]
            publisher_for=_unused_publisher,  # type: ignore[arg-type]
        ),
        RUN_ID,
        ATTEMPT_EPOCH,
    )

    await repository.start_answer_generation()
    await repository.start_answer_generation()
    await repository.authorize_answer_regeneration()
    await repository.authorize_answer_regeneration()

    assert start_calls == 2
    assert authorize_calls == 2
