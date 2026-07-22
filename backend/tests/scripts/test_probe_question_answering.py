"""Probe が final result と event だけを観測する smoke 契約。"""

from __future__ import annotations

import ast
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from typing import get_type_hints

import pytest

from app.agent.planning.contract import TargetTimeWindow

_OVERSIZED_INTEGER_SENTINEL = "91827364554637281928374655463728"
_OVERSIZED_INTEGER_DIGITS = _OVERSIZED_INTEGER_SENTINEL + "7" * (
    5_000 - len(_OVERSIZED_INTEGER_SENTINEL)
)
_OVERSIZED_TIME_WINDOW = (
    '{"kind":"last_n_days","days":' + _OVERSIZED_INTEGER_DIGITS + "}"
)


def _probe_tree() -> ast.Module:
    path = (
        Path(__file__).resolve().parents[2] / "scripts" / "probe_question_answering.py"
    )
    return ast.parse(path.read_text(encoding="utf-8"))


def _probe_module() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[2] / "scripts" / "probe_question_answering.py"
    )
    spec = spec_from_file_location("probe_question_answering_test", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def _function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in tree.body:
        if (
            isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == name
        ):
            return node
    raise AssertionError(f"probe must define {name}")


def _call_name(call: ast.Call) -> str | None:
    match call.func:
        case ast.Name(id=name):
            return name
        case ast.Attribute(attr=name):
            return name
    return None


def _keyword_value(call: ast.Call, name: str) -> ast.expr:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    raise AssertionError(f"call must pass {name}=")


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node) == name
    ]


def _phase_call(function: ast.AsyncFunctionDef | ast.FunctionDef) -> ast.Call:
    phase_calls = _calls(function, "AnsweringPhases")
    assert len(phase_calls) == 1
    return phase_calls[0]


def test_probe_uses_answering_runner_without_removed_external_pipeline_seams() -> None:
    tree = _probe_tree()
    imported = _imported_names(tree)
    loaded = _loaded_names(tree)
    removed = {
        "ExternalSearchResearchRunner",
        "ExternalSearchService",
        "ExternalSearchRequest",
        "ExternalSearchRunResult",
        "ExternalSearchRunner",
        "ExternalPlanSearcher",
        "build_external_search_service",
        "ExternalSearchOutcome",
        "_RecordingExternalSearch",
        "_UnreachableExternalSearch",
    }
    phase_keyword_sets = [
        {keyword.arg for keyword in node.keywords}
        for node in _calls(tree, "AnsweringPhases")
    ]

    assert {
        "AnsweringPhases",
        "AnsweringRunner",
        "DirectAnswerPlan",
        "SearchPlan",
        "TargetTimeWindow",
        "GeminiQueryEmbedder",
        "InternalSearchService",
        "PgVectorArticleSearchRepository",
        "INPUT_SAFETY_AGENT",
        "InputSafetyService",
        "RunContext",
        "RunInput",
        "async_sessionmaker",
        "build_external_research_runtime_factory",
        "engine",
    } <= imported
    assert removed.isdisjoint(imported)
    assert removed.isdisjoint(loaded)
    assert phase_keyword_sets == [
        {
            "planner",
            "internal_search",
            "external_runtime_factory",
            "direct_answerer",
            "evidence_answerer",
        },
        {
            "planner",
            "internal_search",
            "external_runtime_factory",
            "direct_answerer",
            "evidence_answerer",
        },
    ]


def test_probe_parser_and_dispatch_support_only_direct_and_search_modes() -> None:
    tree = _probe_tree()
    parser = _function(tree, "_build_parser")
    mode_arguments = [
        call
        for call in _calls(parser, "add_argument")
        if call.args
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == "--mode"
    ]
    dispatch = _function(tree, "_probe")
    dispatch_calls = {
        _call_name(call) for call in ast.walk(dispatch) if isinstance(call, ast.Call)
    }

    assert len(mode_arguments) == 1
    choices = _keyword_value(mode_arguments[0], "choices")
    assert isinstance(choices, ast.Tuple)
    assert [
        element.value for element in choices.elts if isinstance(element, ast.Constant)
    ] == [
        "direct",
        "search",
    ]
    default = _keyword_value(mode_arguments[0], "default")
    assert isinstance(default, ast.Constant)
    assert default.value == "search"
    assert "external" not in ast.unparse(mode_arguments[0])
    assert {"_probe_direct", "_probe_search"} <= dispatch_calls
    assert "_probe_external" not in dispatch_calls
    assert 'mode == "external"' not in ast.unparse(dispatch)


def test_probe_parser_converts_time_window_json_to_typed_contract_value() -> None:
    parsed = (
        _probe_module()
        ._build_parser()
        .parse_args(["--time-window", '{"kind":"last_n_days","days":7}'])
    )

    assert parsed.time_window == TargetTimeWindow(kind="last_n_days", days=7)


@pytest.mark.parametrize(
    ("raw_time_window", "sensitive_fragment"),
    [
        pytest.param("not-json", "not-json", id="not-json"),
        pytest.param(
            '{"kind":"last_n_days","days":0}',
            '"days":0',
            id="invalid-contract",
        ),
        pytest.param(
            _OVERSIZED_TIME_WINDOW,
            _OVERSIZED_INTEGER_SENTINEL,
            id="oversized-integer",
        ),
    ],
)
def test_probe_parser_rejects_invalid_time_window_without_echoing_input(
    raw_time_window: str,
    sensitive_fragment: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        _probe_module()._build_parser().parse_args(["--time-window", raw_time_window])

    stderr = capsys.readouterr().err
    assert raised.value.code == 2
    assert "time window must be a valid TargetTimeWindow JSON object" in stderr
    assert raw_time_window not in stderr
    assert sensitive_fragment not in stderr


def test_probe_search_path_declares_and_preserves_typed_time_window() -> None:
    module = _probe_module()
    target_time_window = TargetTimeWindow(kind="last_n_days", days=7)
    plan = module._build_search_plan(
        "NVIDIA の直近発表",
        ["NVIDIA の直近発表を確認する"],
        target_time_window=target_time_window,
    )
    time_window_hints = {
        function_name: get_type_hints(getattr(module, function_name))[
            "target_time_window"
        ]
        for function_name in ("_probe", "_probe_search", "_build_search_plan")
    }

    assert plan.target_time_window is target_time_window
    assert time_window_hints == {
        function_name: TargetTimeWindow | None for function_name in time_window_hints
    }


def test_probe_forwards_time_window_through_dispatch_and_search_plan() -> None:
    tree = _probe_tree()

    for caller_name, callee_name in (
        ("_probe", "_probe_search"),
        ("_probe_search", "_build_search_plan"),
    ):
        caller = _function(tree, caller_name)
        calls = _calls(caller, callee_name)
        assert len(calls) == 1
        forwarded = _keyword_value(calls[0], "target_time_window")
        parameter_names = {
            parameter.arg
            for parameter in (
                *caller.args.posonlyargs,
                *caller.args.args,
                *caller.args.kwonlyargs,
            )
        }
        assert isinstance(forwarded, ast.Name)
        assert forwarded.id == "target_time_window"
        assert forwarded.id in parameter_names


def test_search_probe_injects_requested_count_and_events_into_runner() -> None:
    search = _function(_probe_tree(), "_probe_search")
    runner_calls = _calls(search, "AnsweringRunner")

    assert len(runner_calls) == 1
    assert {keyword.arg for keyword in runner_calls[0].keywords} == {
        "input_safety_checker",
        "context_preparer",
        "phases_factory",
        "events",
        "requested_external_agent_count",
    }
    assert "requested_agent_count" in _loaded_names(search)
    assert "events" in _loaded_names(search)


def test_search_probe_passes_actual_internal_and_external_dependencies_to_phases() -> (
    None
):
    search = _function(_probe_tree(), "_probe_search")
    session_factory_calls = _calls(search, "async_sessionmaker")
    internal_service_calls = _calls(search, "InternalSearchService")
    phase = _phase_call(search)

    assert "_UnreachableInternalSearch" not in _loaded_names(search)
    assert len(session_factory_calls) == 1
    assert any(
        isinstance(argument, ast.Name) and argument.id == "engine"
        for argument in (
            [*session_factory_calls[0].args]
            + [keyword.value for keyword in session_factory_calls[0].keywords]
        )
    )
    assert len(internal_service_calls) == 1

    internal_service = internal_service_calls[0]
    embedder = _keyword_value(internal_service, "embedder")
    repository = _keyword_value(internal_service, "article_search_repository")
    events = _keyword_value(internal_service, "events")
    assert isinstance(embedder, ast.Call)
    assert _call_name(embedder) == "GeminiQueryEmbedder"
    assert isinstance(repository, ast.Call)
    assert _call_name(repository) == "PgVectorArticleSearchRepository"
    assert isinstance(events, ast.Name)
    assert events.id == "events"

    session_factory_targets = {
        target.id
        for assignment in ast.walk(search)
        if isinstance(assignment, ast.Assign)
        and isinstance(assignment.value, ast.Call)
        and assignment.value in session_factory_calls
        for target in assignment.targets
        if isinstance(target, ast.Name)
    }
    assert len(repository.args) == 1
    assert isinstance(repository.args[0], ast.Name)
    assert repository.args[0].id in session_factory_targets

    service_targets = {
        target.id
        for assignment in ast.walk(search)
        if isinstance(assignment, ast.Assign) and assignment.value is internal_service
        for target in assignment.targets
        if isinstance(target, ast.Name)
    }
    phase_internal_search = _keyword_value(phase, "internal_search")
    assert isinstance(phase_internal_search, ast.Name)
    assert phase_internal_search.id in service_targets
    external_runtime_factory = _keyword_value(phase, "external_runtime_factory")
    assert isinstance(external_runtime_factory, ast.Call)
    assert (
        _call_name(external_runtime_factory)
        == "build_external_research_runtime_factory"
    )


def test_both_probe_paths_construct_runner_with_input_safety_service() -> None:
    tree = _probe_tree()

    for function_name in ("_probe_direct", "_probe_search"):
        runner_calls = _calls(_function(tree, function_name), "AnsweringRunner")

        assert len(runner_calls) == 1
        input_safety = _keyword_value(runner_calls[0], "input_safety_checker")
        assert isinstance(input_safety, ast.Call)
        assert _call_name(input_safety) == "InputSafetyService"
        assert {keyword.arg for keyword in input_safety.keywords} == {
            "agent",
            "runtime_scope_factory",
        }
        agent = _keyword_value(input_safety, "agent")
        assert isinstance(agent, ast.Name)
        assert agent.id == "INPUT_SAFETY_AGENT"


def test_search_probe_summary_uses_final_result_plan_summary_and_events_only() -> None:
    search = _function(_probe_tree(), "_probe_search")
    names = _loaded_names(search)
    text = ast.unparse(search)

    assert "ExternalSearchOutcome" not in names
    assert "InternalSearchOutcome" not in names
    assert "outcome" not in names
    assert "last_outcome" not in text
    assert "deduplicated_evidence_count" not in text
    assert "effective_agent_count" not in text
    assert "result.retrieval" not in text
    assert "result.plan_summary" in text
    assert "events.events" in text


def test_direct_probe_keeps_dependencies_unreachable_and_uses_plan_summary() -> None:
    tree = _probe_tree()
    direct = _function(tree, "_probe_direct")
    phase = _phase_call(direct)
    names = _loaded_names(direct)
    text = ast.unparse(direct)
    result_printer = ast.unparse(_function(tree, "_print_answer_result"))

    assert "DirectAnswerPlan" in names
    assert "InternalSearchService" not in names
    assert "build_external_research_runtime_factory" not in names
    assert "build_external_search_service" not in names
    assert "DEEPSEEK_API_KEY" not in text
    assert "TAVILY_API_KEY" not in text
    assert "planned_mode" not in text
    assert "result.retrieval" not in text
    assert "result.plan_summary" in result_printer
    assert "result.plan_summary.plan_type" in result_printer

    internal_search = _keyword_value(phase, "internal_search")
    external_runtime_factory = _keyword_value(phase, "external_runtime_factory")
    evidence_answerer = _keyword_value(phase, "evidence_answerer")
    direct_answerer = _keyword_value(phase, "direct_answerer")
    assert isinstance(internal_search, ast.Call)
    assert _call_name(internal_search) == "_UnreachableInternalSearch"
    assert isinstance(external_runtime_factory, ast.Call)
    assert _call_name(external_runtime_factory) == "_UnreachableExternalRuntimeFactory"
    assert isinstance(evidence_answerer, ast.Call)
    assert _call_name(evidence_answerer) == "_UnreachableEvidenceAnswerer"
    assert isinstance(direct_answerer, ast.Call)
    assert _call_name(direct_answerer) == "DirectAnswerFlow"
    direct_agent = _keyword_value(direct_answerer, "agent")
    direct_runtime_scope_factory = _keyword_value(
        direct_answerer,
        "runtime_scope_factory",
    )
    assert isinstance(direct_agent, ast.Name)
    assert direct_agent.id == "DIRECT_ANSWER_AGENT"
    assert isinstance(direct_runtime_scope_factory, ast.Name)
    assert direct_runtime_scope_factory.id == "activate_gemini_agent_runtime"


def test_probe_uses_direct_answer_and_search_plans_without_legacy_plan_paths() -> None:
    tree = _probe_tree()
    direct = _function(tree, "_probe_direct")
    search_plan = _function(tree, "_build_search_plan")
    top_level_names = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef, ast.ClassDef))
    }
    legacy_plan_paths = {
        "ExternalSearchPlan",
        "NoRetrievalPlan",
        "_FixedExternalPlanner",
        "_build_external_plan",
        "_probe_external",
    }

    assert "DirectAnswerPlan" in _loaded_names(direct)
    assert "SearchPlan" in _loaded_names(search_plan)
    assert legacy_plan_paths.isdisjoint(_imported_names(tree))
    assert legacy_plan_paths.isdisjoint(_loaded_names(tree))
    assert legacy_plan_paths.isdisjoint(top_level_names)
