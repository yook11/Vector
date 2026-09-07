"""両回答経路が生成を待たせず初回開始後の回収確認を予約する。"""

import asyncio
from unittest.mock import Mock

import pytest

from app.agent.answering import timing
from app.agent.contract import AnswerGenerationStopped
from app.agent.running.deadline.scheduling import AgentDeadlineScheduler
from app.agent.runs.execution import Stop, StopReason
from tests.agent.answering.direct_answer import test_service as direct
from tests.agent.answering.evidence_answer import test_service as evidence
from tests.agent.run_deadline.test_scheduling import RecordingSource
from tests.agent.running._harness import (
    AS_OF,
    RUN_ID,
    ScriptedAnswerGenerationRepository,
)


def make_service(kind, repository, schedule, retry=False):
    outputs = ([" "] if retry else []) + ["回答です。"]
    if kind == "direct":
        runtime = direct.ScriptedStreamingRuntime(outputs)
        return (
            direct.DirectAnswerService(
                agent=direct.DIRECT_ANSWER_AGENT,
                runtime_scope_factory=direct._runtime_scope(runtime),
                repository=repository,
                schedule_deadline_check=schedule,
            ),
            direct._input(),
            "_generate_draft",
        )
    runtime = evidence.FakeGenerator(outputs)
    return (
        evidence.EvidenceAnswerService(
            agent=evidence.EVIDENCE_ANSWER_AGENT,
            runtime_scope_factory=runtime.activate,
            repository=repository,
            schedule_deadline_check=schedule,
        ),
        evidence.EvidenceAnswerInput(
            request=evidence._request(),
            evidence=(),
            target_time_window=None,
            review_missing=(),
        ),
        "_generate_strict_draft",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["direct", "evidence"])
@pytest.mark.parametrize("retry", [False, True])
async def test_generation_finishes_while_single_recovery_reservation_is_blocked(
    kind, retry, monkeypatch
):
    # 予約待機中に生成を完了し、再生成があっても予約を増やさない。
    entered = asyncio.Event()
    source = RecordingSource(stall=True)
    original_add = source.add_schedule

    async def add(schedule):
        entered.set()
        await original_add(schedule)

    source.add_schedule = add
    scheduler = AgentDeadlineScheduler(source)
    schedule = Mock(side_effect=scheduler.reserve_in_background)
    repository = ScriptedAnswerGenerationRepository()
    service, input, method = make_service(kind, repository, schedule, retry)
    original_generate = getattr(service, method)

    async def generate(**kwargs):
        await entered.wait()
        return await original_generate(**kwargs)

    monkeypatch.setattr(service, method, generate)
    try:
        result = await asyncio.wait_for(service.answer(input), 1)
        assert result.answer == "回答です。"
        schedule.assert_called_once_with(
            RUN_ID, AS_OF + timing.answer_generation_recovery_window()
        )
        assert len(scheduler._pending) == 1
        assert repository.authorize_calls == int(retry)
    finally:
        await scheduler.cancel_pending_reservations()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["direct", "evidence"])
@pytest.mark.parametrize(
    "failure", [Stop(StopReason.NOT_CURRENT), RuntimeError("commit failed")]
)
async def test_rejected_or_failed_start_never_schedules(kind, failure):
    # 開始が確定しなければ予約も生成も起動しない。
    schedule = Mock()
    service, input, _ = make_service(
        kind, ScriptedAnswerGenerationRepository(start=failure), schedule
    )
    with pytest.raises(
        AnswerGenerationStopped if isinstance(failure, Stop) else RuntimeError
    ):
        await service.answer(input)
    schedule.assert_not_called()
