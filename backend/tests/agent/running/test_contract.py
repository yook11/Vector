"""AnsweringRunner の public internal contract tests。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, is_dataclass
from datetime import UTC, datetime
from typing import Any, get_type_hints
from uuid import UUID

import pytest

import app.agent.running as running_module
from app.agent.contract import AnswerQuestionResult
from app.agent.research_handoff import (
    ResearchHandoff,
    ResearchRunRecord,
    ResearchTaskRecord,
)
from app.agent.running import (
    RunIdentity,
    RunInput,
    RunResult,
)
from app.agent.threads.contracts import ThreadMessageSnapshot
from tests.agent.running._harness import THREAD_ID, USER_ID

PUBLIC_CONTRACTS = {
    "AnsweringRunner",
    "RunIdentity",
    "RunInput",
    "RunResult",
}


def _field_contract(contract_type: type[Any]) -> tuple[tuple[str, Any], ...]:
    type_hints = get_type_hints(contract_type)
    return tuple(
        (field.name, type_hints[field.name]) for field in fields(contract_type)
    )


def _is_frozen_and_slotted(instance: object) -> bool:
    contract_type = type(instance)
    return (
        is_dataclass(contract_type)
        and contract_type.__dataclass_params__.frozen
        and "__slots__" in contract_type.__dict__
        and not hasattr(instance, "__dict__")
    )


def test_running_package_exports_public_contracts() -> None:
    running = running_module

    assert (
        PUBLIC_CONTRACTS <= set(running.__all__),
        all(getattr(running, name, None) is not None for name in PUBLIC_CONTRACTS),
        "Runner" not in running.__all__,
        not hasattr(running, "Runner"),
        not hasattr(running, "AnsweringRunContext"),
    ) == (
        True,
        True,
        True,
        True,
        True,
    )


def test_run_input_is_frozen_slotted_question_history_and_handoff() -> None:
    """RunInputは質問・履歴に加え、同threadの調査の申し送りを持つ。"""
    run_input_type = RunInput
    history = (
        ThreadMessageSnapshot(role="user", content="前の質問"),
        ThreadMessageSnapshot(role="assistant", content="前の回答"),
    )
    handoff = _handoff()
    run_input = run_input_type(question="続けて説明して", history=history)
    run_input_with_handoff = run_input_type(
        question="続けて説明して",
        history=history,
        research_handoff=handoff,
    )

    with pytest.raises(FrozenInstanceError):
        run_input.question = "変更後の質問"

    assert (
        _field_contract(run_input_type),
        _is_frozen_and_slotted(run_input),
        run_input.question,
        run_input.history,
        run_input.research_handoff,
        run_input_with_handoff.research_handoff,
    ) == (
        (
            ("question", str),
            ("history", tuple[ThreadMessageSnapshot, ...]),
            ("research_handoff", ResearchHandoff | None),
        ),
        True,
        "続けて説明して",
        history,
        None,
        handoff,
    )


def test_run_identity_is_frozen_slotted_ids_and_time() -> None:
    identity_type = RunIdentity
    run_id = UUID("019bd239-1ed4-7fbb-a336-04fe3c197645")
    as_of = datetime(2026, 7, 16, 9, 30, tzinfo=UTC)
    identity = identity_type(
        user_id=USER_ID,
        run_id=run_id,
        thread_id=THREAD_ID,
        as_of=as_of,
    )

    with pytest.raises(FrozenInstanceError):
        identity.as_of = datetime(2026, 7, 16, 9, 31, tzinfo=UTC)

    assert (
        _field_contract(identity_type),
        _is_frozen_and_slotted(identity),
        not hasattr(identity, "attempt_epoch"),
        identity.user_id,
        identity.run_id,
        identity.thread_id,
        identity.as_of,
    ) == (
        (
            ("user_id", UUID),
            ("run_id", UUID),
            ("thread_id", UUID),
            ("as_of", datetime),
        ),
        True,
        True,
        USER_ID,
        run_id,
        THREAD_ID,
        as_of,
    )


def _handoff() -> ResearchHandoff:
    return ResearchHandoff(
        updated_at=datetime(2026, 7, 16, 9, 30, tzinfo=UTC),
        runs=(
            ResearchRunRecord(
                as_of=datetime(2026, 7, 16, 9, 30, tzinfo=UTC),
                tasks=(
                    ResearchTaskRecord(
                        research_goal="調査目標",
                        executed_queries=("q",),
                    ),
                ),
            ),
        ),
    )


def test_run_result_is_frozen_slotted_output_and_handoff() -> None:
    """RunResultはfinal_outputとoptionalなresearch_handoffだけを持つ。"""
    run_result_type = RunResult
    final_output = AnswerQuestionResult.model_construct()
    run_result = run_result_type(final_output=final_output)
    handoff = _handoff()
    run_result_with_handoff = run_result_type(
        final_output=final_output,
        research_handoff=handoff,
    )

    with pytest.raises(FrozenInstanceError):
        run_result.final_output = final_output
    with pytest.raises(TypeError):
        run_result_type(final_output=final_output, research_checkpoint=object())

    assert (
        _field_contract(run_result_type),
        _is_frozen_and_slotted(run_result),
        run_result.final_output is final_output,
        run_result.research_handoff,
        run_result_with_handoff.research_handoff is handoff,
        not hasattr(run_result, "research_checkpoint"),
        not hasattr(run_result, "previous_answer"),
        not hasattr(run_result, "identity"),
    ) == (
        (
            ("final_output", AnswerQuestionResult),
            ("research_handoff", ResearchHandoff | None),
        ),
        True,
        True,
        None,
        True,
        True,
        True,
        True,
    )
