"""AnsweringRunner の public internal contract tests。"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import FrozenInstanceError, fields, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, get_type_hints
from uuid import UUID

import pytest

from app.agent.answering.direct_answer.contract import DirectAnswerer
from app.agent.answering.evidence_answer.contract import EvidenceAnswerer
from app.agent.contract import AnswerQuestionResult
from app.agent.evidence_collection.contract import InternalArticleRetriever
from app.agent.evidence_collection.external_search import ExternalResearchRuntimeFactory
from app.agent.planning.contract import QuestionPlanner
from app.agent.question_context import (
    QuestionContext,
    QuestionContextPreparationResult,
)
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


def _method_contract(
    method: Any,
) -> tuple[tuple[tuple[str, inspect._ParameterKind, Any, bool], ...], Any]:
    signature = inspect.signature(method)
    type_hints = get_type_hints(method)
    parameters = tuple(
        (
            parameter.name,
            parameter.kind,
            type_hints.get(parameter.name),
            parameter.default is inspect.Parameter.empty,
        )
        for parameter in signature.parameters.values()
    )
    return parameters, type_hints["return"]


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
    run_input_type = _contract_type("RunInput")
    history = (
        ThreadMessageSnapshot(role="user", content="前の質問"),
        ThreadMessageSnapshot(role="assistant", content="前の回答"),
    )
    run_input = run_input_type(question="続けて説明して", history=history)

    with pytest.raises(FrozenInstanceError):
        run_input.question = "変更後の質問"

    assert (
        _field_contract(run_input_type),
        _is_frozen_and_slotted(run_input),
        run_input.question,
        run_input.history,
    ) == (
        (
            ("question", str),
            ("history", tuple[ThreadMessageSnapshot, ...]),
        ),
        True,
        "続けて説明して",
        history,
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

    with pytest.raises(FrozenInstanceError):
        run_result.context = answering_context

    assert (
        _field_contract(run_result_type),
        _is_frozen_and_slotted(run_result),
        run_result.final_output is final_output,
        run_result.context is answering_context,
    ) == (
        (
            ("final_output", AnswerQuestionResult),
            ("context", answering_context_type),
        ),
        True,
        True,
        True,
    )


def test_question_context_preparer_protocol_has_only_required_inputs() -> None:
    preparer_type = _contract_type("QuestionContextPreparer")

    assert (
        getattr(preparer_type, "_is_protocol", False),
        inspect.iscoroutinefunction(preparer_type.prepare),
        _method_contract(preparer_type.prepare),
    ) == (
        True,
        True,
        (
            (
                (
                    "self",
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    None,
                    True,
                ),
                ("question", inspect.Parameter.KEYWORD_ONLY, str, True),
                (
                    "history",
                    inspect.Parameter.KEYWORD_ONLY,
                    list[ThreadMessageSnapshot],
                    True,
                ),
                ("as_of", inspect.Parameter.KEYWORD_ONLY, datetime, True),
                ("run_id", inspect.Parameter.KEYWORD_ONLY, UUID, True),
            ),
            QuestionContextPreparationResult,
        ),
    )


def test_run_hooks_protocol_exposes_only_resolved_question_projection() -> None:
    hooks_type = _contract_type("RunHooks")

    assert (
        getattr(hooks_type, "_is_protocol", False),
        inspect.iscoroutinefunction(hooks_type.on_answering_context_prepared),
        _method_contract(hooks_type.on_answering_context_prepared),
    ) == (
        True,
        True,
        (
            (
                (
                    "self",
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    None,
                    True,
                ),
                (
                    "original_question",
                    inspect.Parameter.KEYWORD_ONLY,
                    str,
                    True,
                ),
                ("has_history", inspect.Parameter.KEYWORD_ONLY, bool, True),
                (
                    "question_context",
                    inspect.Parameter.KEYWORD_ONLY,
                    QuestionContext,
                    True,
                ),
            ),
            type(None),
        ),
    )


def test_answering_phases_owns_runtime_without_external_search_port() -> None:
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
            ("internal_search", InternalArticleRetriever),
            ("external_runtime_factory", ExternalResearchRuntimeFactory),
            ("direct_answerer", DirectAnswerer),
            ("evidence_answerer", EvidenceAnswerer),
        ),
        (
            "planner",
            "internal_search",
            "external_runtime_factory",
            "direct_answerer",
            "evidence_answerer",
        ),
        (True, True, True, True, True),
    )


def test_removed_external_pipeline_symbols_are_absent() -> None:
    app_root = Path(__file__).resolve().parents[3] / "app" / "agent"
    forbidden = {
        "ExternalSearchResearchRunner",
        "ExternalSearchService",
        "ExternalSearchRequest",
        "ExternalSearchRunResult",
        "ExternalSearchRunner",
        "ExternalPlanSearcher",
        "build_external_search_service",
    }

    assert all(
        symbol not in path.read_text(encoding="utf-8")
        for path in app_root.rglob("*.py")
        for symbol in forbidden
    )


def test_evidence_collection_package_has_no_legacy_collector_symbols() -> None:
    package_root = (
        Path(__file__).resolve().parents[3] / "app" / "agent" / "evidence_collection"
    )
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in package_root.glob("*.py")
    )

    assert (
        "EvidenceCollector" not in source and "EvidenceCollectionService" not in source
    )
