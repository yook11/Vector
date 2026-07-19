"""Question-answering probe の S1 external runtime wiring 契約。"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.agent.answering.direct_answer.contract import DirectAnswerDraft
from app.agent.answering.evidence_answer.contract import EvidenceAnswerDraft
from app.agent.contract import AnswerQuestionResult
from app.agent.evidence_collection.external_search import (
    ExternalSearchOutcome,
    ResearchTaskReport,
)
from app.agent.planning.contract import ExternalResearchTask
from app.agent.question_context import (
    QuestionContext,
    QuestionContextPreparationResult,
    QuestionContextTelemetry,
)
from scripts import probe_question_answering as probe


class _ContextPreparer:
    async def prepare(self, **_kwargs: object) -> QuestionContextPreparationResult:
        return QuestionContextPreparationResult(
            context=QuestionContext(standalone_question="NVIDIA の見通しは？"),
            telemetry=QuestionContextTelemetry(),
        )


class _EvidenceAnswerer:
    async def answer(self, **_kwargs: object) -> EvidenceAnswerDraft:
        return EvidenceAnswerDraft(
            sufficiency="insufficient",
            answer="取得できた根拠では十分に回答できません。",
            missing_aspects=["追加の根拠が必要です"],
        )


class _DirectAnswerer:
    async def answer(self, **_kwargs: object) -> DirectAnswerDraft:
        return DirectAnswerDraft(answer="Vector の使い方です。")


class _ExternalSearchService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.outcome: ExternalSearchOutcome | None = None

    async def search(
        self,
        external_research_tasks: list[ExternalResearchTask],
        **kwargs: object,
    ) -> ExternalSearchOutcome:
        self.calls.append(
            {
                "tasks": external_research_tasks,
                **kwargs,
            }
        )
        self.outcome = ExternalSearchOutcome(
            tasks=external_research_tasks,
            task_reports=[
                ResearchTaskReport(
                    task_index=index,
                    collection_goal=task.collection_goal,
                    status="succeeded",
                )
                for index, task in enumerate(external_research_tasks)
            ],
            requested_agent_count=kwargs["requested_agent_count"],  # type: ignore[arg-type]
            effective_agent_count=len(external_research_tasks),
        )
        return self.outcome


class _ExternalRuntimeScope:
    def __init__(self, runtime: object) -> None:
        self._runtime = runtime
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> object:
        self.entered = True
        return self._runtime

    async def __aexit__(
        self,
        exc_type: object,
        exc: BaseException | None,
        traceback: object,
    ) -> bool:
        self.exited = True
        return False


class _ExternalRuntimeFactory:
    def __init__(self, runtime: object) -> None:
        self.scope = _ExternalRuntimeScope(runtime)
        self.activate_calls = 0

    def activate(self) -> _ExternalRuntimeScope:
        self.activate_calls += 1
        return self.scope


def _probe_tree() -> ast.Module:
    probe_path = (
        Path(__file__).resolve().parents[2] / "scripts" / "probe_question_answering.py"
    )
    return ast.parse(probe_path.read_text(encoding="utf-8"))


def _imported_names(tree: ast.Module) -> set[str]:
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }


def _loaded_names(tree: ast.AST) -> set[str]:
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


def _function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"probe must define {name}")


def test_probe_uses_runner_dependencies_without_legacy_collector_boundary() -> None:
    tree = _probe_tree()
    imported_names = _imported_names(tree)
    loaded_names = _loaded_names(tree)
    legacy_boundaries = {
        "AnswerQuestionInput",
        "QuestionAnsweringAgent",
        "QuestionAnsweringOrchestrator",
        "build_question_answering_starting_agent",
        "build_question_answering_agent",
        "starting_agent",
        "GeminiDirectAnswerGenerator",
        "GeminiEvidenceAnswerDraftGenerator",
        "EvidenceCollector",
        "EvidenceCollectionService",
        "_RecordingEvidenceCollector",
        "_UnreachableEvidenceCollector",
    }
    phase_keyword_sets = [
        {keyword.arg for keyword in node.keywords}
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "AnsweringPhases"
    ]

    assert (
        {
            "AnsweringPhases",
            "AnsweringRunner",
            "RunContext",
            "RunInput",
            "build_external_search_service",
            "build_external_research_runtime_factory",
        }
        <= imported_names,
        legacy_boundaries.isdisjoint(imported_names),
        legacy_boundaries.isdisjoint(loaded_names),
        phase_keyword_sets
        == [
            {
                "planner",
                "internal_search",
                "external_search",
                "external_runtime_factory",
                "direct_answerer",
                "evidence_answerer",
            },
            {
                "planner",
                "internal_search",
                "external_search",
                "external_runtime_factory",
                "direct_answerer",
                "evidence_answerer",
            },
        ],
    ) == (True, True, True, True)


def test_probe_binds_agents_to_service_per_call_and_keeps_summary_input() -> None:
    tree = _probe_tree()
    requested_count_forwarders = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and "requested_agent_count"
        in {argument.arg for argument in node.args.args + node.args.kwonlyargs}
        and any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "search"
            and any(keyword.arg == "requested_agent_count" for keyword in call.keywords)
            for call in ast.walk(node)
        )
    ]
    summary_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_print_retrieval_summary"
    ]

    assert (
        len(requested_count_forwarders),
        len(summary_calls),
        any(
            keyword.arg == "requested_agent_count"
            for keyword in summary_calls[0].keywords
        ),
    ) == (1, 1, True)


def test_direct_probe_uses_unreachable_external_dependencies() -> None:
    direct = _function(_probe_tree(), "_probe_direct")
    direct_names = _loaded_names(direct)
    direct_text = ast.unparse(direct)

    assert (
        {"_UnreachableExternalSearch", "_UnreachableExternalRuntimeFactory"}
        <= direct_names,
        "build_external_search_service" not in direct_names,
        "build_external_research_runtime_factory" not in direct_names,
        "DEEPSEEK_API_KEY" not in direct_text,
        "TAVILY_API_KEY" not in direct_text,
    ) == (True, True, True, True, True)


@pytest.mark.asyncio
async def test_direct_probe_completes_without_external_provider_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _OpaqueCredential:
        def get_secret_value(self) -> object:
            return object()

    class _DirectProbeSettings:
        gemini_api_key = _OpaqueCredential()

        @property
        def deepseek_api_key(self) -> object:
            raise AssertionError("direct probe must not read DeepSeek credentials")

        @property
        def tavily_api_key(self) -> object:
            raise AssertionError("direct probe must not read Tavily credentials")

    def external_builder_must_not_run() -> object:
        raise AssertionError("direct probe must not build external providers")

    required_secret_names: list[str] = []
    answer_calls: list[AnswerQuestionResult] = []
    runtime_factory = _ExternalRuntimeFactory(object())

    monkeypatch.setattr(probe, "settings", _DirectProbeSettings())
    monkeypatch.setattr(
        probe,
        "_require_secret",
        lambda name, _value: required_secret_names.append(name),
    )
    monkeypatch.setattr(
        probe,
        "QuestionContextService",
        lambda **_kwargs: _ContextPreparer(),
    )
    monkeypatch.setattr(
        probe,
        "DirectAnswerFlow",
        lambda **_kwargs: _DirectAnswerer(),
    )
    monkeypatch.setattr(
        probe,
        "_UnreachableExternalRuntimeFactory",
        lambda: runtime_factory,
    )
    monkeypatch.setattr(
        probe,
        "build_external_search_service",
        external_builder_must_not_run,
    )
    monkeypatch.setattr(
        probe,
        "build_external_research_runtime_factory",
        external_builder_must_not_run,
    )
    monkeypatch.setattr(probe, "_print_answer_result", answer_calls.append)

    await probe._probe_direct(question="Vector の使い方を教えて")

    assert (
        required_secret_names,
        runtime_factory.activate_calls,
        runtime_factory.scope.entered,
        runtime_factory.scope.exited,
        len(answer_calls),
        answer_calls[0].answer,
    ) == (["GEMINI_API_KEY"], 0, False, False, 1, "Vector の使い方です。")


@pytest.mark.asyncio
async def test_external_probe_reports_runner_outcome_without_real_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _ExternalSearchService()
    borrowed_runtime = object()
    runtime_factory = _ExternalRuntimeFactory(borrowed_runtime)
    summary_calls: list[dict[str, object]] = []
    answer_calls: list[object] = []

    monkeypatch.setattr(probe, "_require_secret", lambda *_args: None)
    monkeypatch.setattr(
        probe,
        "QuestionContextService",
        lambda **_kwargs: _ContextPreparer(),
    )
    monkeypatch.setattr(
        probe,
        "EvidenceAnswerFlow",
        lambda **_kwargs: _EvidenceAnswerer(),
    )
    monkeypatch.setattr(probe, "build_external_search_service", lambda: service)
    monkeypatch.setattr(
        probe,
        "build_external_research_runtime_factory",
        lambda: runtime_factory,
    )
    monkeypatch.setattr(
        probe,
        "_print_retrieval_summary",
        lambda **kwargs: summary_calls.append(kwargs),
    )
    monkeypatch.setattr(
        probe,
        "_print_answer_result",
        lambda result: answer_calls.append(result),
    )

    await probe._probe_external(
        question="NVIDIA の見通しは？",
        goals=["供給を確認する", "需要を確認する"],
        requested_agent_count=2,
        target_time_window="直近24時間",
    )

    service_call = service.calls[0]
    summary_call = summary_calls[0]
    answer_result = answer_calls[0]
    assert (
        len(service.calls),
        service_call["requested_agent_count"],
        service_call["external"] is borrowed_runtime,
        runtime_factory.scope.entered,
        runtime_factory.scope.exited,
        len(summary_calls),
        summary_call["outcome"] is service.outcome,
        summary_call["requested_agent_count"],
        summary_call["collection_failures"],
        summary_call["collection_failures"]
        == answer_result.retrieval.collection_failures,  # type: ignore[union-attr]
        len(answer_calls),
    ) == (1, 2, True, True, True, 1, True, 2, [], True, 1)
