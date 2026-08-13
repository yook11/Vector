"""AnsweringRunner の public internal contract tests。"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import FrozenInstanceError, fields, is_dataclass
from datetime import UTC, datetime
from types import ModuleType
from typing import Any, get_type_hints
from uuid import UUID

import pytest

from app.agent.answering.direct_answer.contract import DirectAnswerer
from app.agent.answering.evidence_answer.contract import EvidenceAnswerer
from app.agent.contract import AnswerQuestionResult
from app.agent.evidence_collection import NewsCollector
from app.agent.evidence_collection.external_search import ExternalResearchRuntimeFactory
from app.agent.evidence_review import EvidenceReviewer
from app.agent.planning.contract import QuestionPlanner
from app.agent.question_context import QuestionContext
from app.agent.research_checkpoint import ResearchCheckpoint, ResearchTaskRecord
from app.agent.threads.contracts import ThreadMessageSnapshot

RUNNING_MODULE = "app.agent.running"
PUBLIC_CONTRACTS = {
    "AnsweringRunner",
    "AnsweringRunContext",
    "QuestionContextPreparer",
    "RunContext",
    "RunHooks",
    "RunInput",
    "RunResult",
}


def _running_module() -> ModuleType:
    missing_contract = False
    try:
        return importlib.import_module(RUNNING_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name == RUNNING_MODULE or exc.name.startswith(f"{RUNNING_MODULE}."):
            missing_contract = True
        else:
            raise
    if missing_contract:
        pytest.fail(
            "app.agent.running の public internal contract が未実装です",
            pytrace=False,
        )
    raise AssertionError("unreachable")


def _contract_type(name: str) -> type[Any]:
    contract_type = getattr(_running_module(), name, None)
    if contract_type is None:
        pytest.fail(f"app.agent.running must export {name}", pytrace=False)
    return contract_type


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


def _run_context() -> object:
    run_context_type = _contract_type("RunContext")
    return run_context_type(
        run_id=UUID("019bd239-1ed4-7fbb-a336-04fe3c197645"),
        as_of=datetime(2026, 7, 16, 9, 30, tzinfo=UTC),
    )


def test_running_package_exports_public_contracts() -> None:
    running = _running_module()

    assert (
        PUBLIC_CONTRACTS <= set(running.__all__),
        all(getattr(running, name, None) is not None for name in PUBLIC_CONTRACTS),
        "Runner" not in running.__all__,
        not hasattr(running, "Runner"),
    ) == (
        True,
        True,
        True,
        True,
    )


def test_run_input_is_frozen_slotted_question_and_tuple_history() -> None:
    """agent-research-checkpoint-context-slice: RunInputは既存2 fieldに加え、

    同threadの直近checkpointを渡す`prior_research`(既定は空tuple)を持つ。
    """
    run_input_type = _contract_type("RunInput")
    history = (
        ThreadMessageSnapshot(role="user", content="前の質問"),
        ThreadMessageSnapshot(role="assistant", content="前の回答"),
    )
    checkpoint = ResearchCheckpoint(
        as_of=datetime(2026, 7, 16, 9, 30, tzinfo=UTC),
        tasks=(
            ResearchTaskRecord(
                research_goal="調査目標",
                executed_queries=("q",),
                adopted_claims=(),
            ),
        ),
        unresolved_after_search=(),
    )
    run_input = run_input_type(question="続けて説明して", history=history)
    run_input_with_prior_research = run_input_type(
        question="続けて説明して",
        history=history,
        prior_research=(checkpoint,),
    )

    with pytest.raises(FrozenInstanceError):
        run_input.question = "変更後の質問"

    assert (
        _field_contract(run_input_type),
        _is_frozen_and_slotted(run_input),
        run_input.question,
        run_input.history,
        run_input.prior_research,
        run_input_with_prior_research.prior_research,
    ) == (
        (
            ("question", str),
            ("history", tuple[ThreadMessageSnapshot, ...]),
            ("prior_research", tuple[ResearchCheckpoint, ...]),
        ),
        True,
        "続けて説明して",
        history,
        (),
        (checkpoint,),
    )


def test_run_context_is_frozen_slotted_run_identity_and_time() -> None:
    run_context_type = _contract_type("RunContext")
    run_id = UUID("019bd239-1ed4-7fbb-a336-04fe3c197645")
    as_of = datetime(2026, 7, 16, 9, 30, tzinfo=UTC)
    run_context = run_context_type(run_id=run_id, as_of=as_of)

    with pytest.raises(FrozenInstanceError):
        run_context.as_of = datetime(2026, 7, 16, 9, 31, tzinfo=UTC)

    assert (
        _field_contract(run_context_type),
        _is_frozen_and_slotted(run_context),
        run_context.run_id,
        run_context.as_of,
    ) == (
        (("run_id", UUID), ("as_of", datetime)),
        True,
        run_id,
        as_of,
    )


def test_answering_context_requires_prepared_question_context() -> None:
    answering_context_type = _contract_type("AnsweringRunContext")
    run_context = _run_context()
    question_context = QuestionContext(standalone_question="NVIDIA の直近発表は？")
    answering_context = answering_context_type(
        run_context=run_context,
        question_context=question_context,
        previous_answer="前回の回答本文",
    )

    with pytest.raises(TypeError):
        answering_context_type(
            run_context=run_context,
            previous_answer="前回の回答本文",
        )
    with pytest.raises(TypeError):
        answering_context_type(
            run=run_context,
            question_context=question_context,
            previous_answer="前回の回答本文",
        )
    with pytest.raises(FrozenInstanceError):
        answering_context.previous_answer = "変更後の回答本文"

    assert (
        _field_contract(answering_context_type),
        _is_frozen_and_slotted(answering_context),
        answering_context.run_context is run_context,
        not hasattr(answering_context, "run"),
        answering_context.question_context is question_context,
        answering_context.previous_answer,
    ) == (
        (
            ("run_context", _contract_type("RunContext")),
            ("question_context", QuestionContext),
            ("previous_answer", str),
        ),
        True,
        True,
        True,
        True,
        "前回の回答本文",
    )


def test_run_result_is_frozen_slotted_output_and_answering_context() -> None:
    """agent-research-checkpoint-context-slice: RunResultは既存2 fieldに加え、
    外部検索を実行しなかったRunではNoneのままの`research_checkpoint`
    (default None)を持つ。フィールド完全一致を固定するcontract testのため、
    新契約は3 fieldの完全一致で表す。
    """
    answering_context_type = _contract_type("AnsweringRunContext")
    run_result_type = _contract_type("RunResult")
    answering_context = answering_context_type(
        run_context=_run_context(),
        question_context=QuestionContext(standalone_question="NVIDIA の直近発表は？"),
        previous_answer="",
    )
    final_output = AnswerQuestionResult.model_construct()
    run_result = run_result_type(
        final_output=final_output,
        context=answering_context,
    )
    checkpoint = ResearchCheckpoint(
        as_of=datetime(2026, 7, 16, 9, 30, tzinfo=UTC),
        tasks=(
            ResearchTaskRecord(
                research_goal="調査目標",
                executed_queries=("q",),
                adopted_claims=(),
            ),
        ),
        unresolved_after_search=(),
    )
    run_result_with_checkpoint = run_result_type(
        final_output=final_output,
        context=answering_context,
        research_checkpoint=checkpoint,
    )

    with pytest.raises(FrozenInstanceError):
        run_result.context = answering_context

    assert (
        _field_contract(run_result_type),
        _is_frozen_and_slotted(run_result),
        run_result.final_output is final_output,
        run_result.context is answering_context,
        run_result.research_checkpoint,
        run_result_with_checkpoint.research_checkpoint is checkpoint,
    ) == (
        (
            ("final_output", AnswerQuestionResult),
            ("context", answering_context_type),
            ("research_checkpoint", ResearchCheckpoint | None),
        ),
        True,
        True,
        True,
        None,
        True,
    )


def test_answering_phases_owns_collector_without_internal_search_port() -> None:
    """保証するテスト条件 18: AnsweringPhasesはcollector fieldでRun単位収集を持つ。

    D4-T3: 精査は EvidenceReviewer が起動するため、reviewer field を追加で持つ
    (他のphase roleと同様、明示的な配線を要求するためdefaultは持たない)。
    """
    phases_type = _contract_type("AnsweringPhases")
    signature = inspect.signature(phases_type)

    assert (
        _field_contract(phases_type),
        tuple(signature.parameters),
        tuple(
            parameter.default is inspect.Parameter.empty
            for parameter in signature.parameters.values()
        ),
    ) == (
        (
            ("planner", QuestionPlanner),
            ("collector", NewsCollector),
            ("external_runtime_factory", ExternalResearchRuntimeFactory),
            ("direct_answerer", DirectAnswerer),
            ("evidence_answerer", EvidenceAnswerer),
            ("reviewer", EvidenceReviewer),
        ),
        (
            "planner",
            "collector",
            "external_runtime_factory",
            "direct_answerer",
            "evidence_answerer",
            "reviewer",
        ),
        (True, True, True, True, True, True),
    )
