"""回答生成の口(AgentRunAnswerGenerationGate)の開始・書き直しの問い合わせ方。

前提: DB は使わず、AgentRunStateService の操作を差し替える。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.agent.live_updates.reporters import AgentRunLiveStageReporter
from app.agent.running.answer_generation import AgentRunAnswerGenerationGate
from app.agent.running.state import AnswerGenerationStarted, CanContinue
from app.agent.running.state_service import AgentRunStateService

RUN_ID = UUID("019bd239-1ed4-7fbb-a336-04fe3c197652")
ATTEMPT_EPOCH = 3
STARTED_AT = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def _unused_session_factory() -> object:
    raise AssertionError("state service operations are replaced in this test")


def _unused_publisher(_run_id: UUID, _attempt_epoch: int) -> object:
    raise AssertionError("no run is closed in this test")


class _DiscardingPublisher:
    async def publish(self, _event: object) -> str | None:
        return None


@pytest.mark.asyncio
async def test_start_and_regenerate_ask_the_state_service_every_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """開始と書き直しは結果を覚えず、呼ぶたびにRunの状態を変える操作へ問い合わせる。"""
    start_calls = 0
    regenerate_calls = 0

    async def start_once(*_args: object, **_kwargs: object) -> AnswerGenerationStarted:
        nonlocal start_calls
        start_calls += 1
        return AnswerGenerationStarted(RUN_ID, STARTED_AT)

    async def authorize_once(*_args: object, **_kwargs: object) -> CanContinue:
        nonlocal regenerate_calls
        regenerate_calls += 1
        return CanContinue()

    monkeypatch.setattr(AgentRunStateService, "start_answer_generation", start_once)
    monkeypatch.setattr(
        AgentRunStateService, "authorize_answer_regeneration", authorize_once
    )
    gate = AgentRunAnswerGenerationGate(
        AgentRunStateService(
            session_factory=_unused_session_factory,  # type: ignore[arg-type]
            publisher_for=_unused_publisher,  # type: ignore[arg-type]
        ),
        RUN_ID,
        ATTEMPT_EPOCH,
        schedule_deadline_check=lambda *_: None,
        stage_reporter=AgentRunLiveStageReporter(_DiscardingPublisher()),  # type: ignore[arg-type]
    )

    await gate.start()
    await gate.start()
    await gate.regenerate()
    await gate.regenerate()

    assert (start_calls, regenerate_calls) == (2, 2)
