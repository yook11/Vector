"""Probe が final result と event だけを観測する smoke 契約。"""

from __future__ import annotations

import ast
from pathlib import Path


def _probe_tree() -> ast.Module:
    path = (
        Path(__file__).resolve().parents[2] / "scripts" / "probe_question_answering.py"
    )
    return ast.parse(path.read_text(encoding="utf-8"))


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
            "build_external_research_runtime_factory",
        }
        <= imported,
        removed.isdisjoint(imported),
        removed.isdisjoint(loaded),
        phase_keyword_sets
        == [
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
        ],
    ) == (True, True, True, True)


def test_external_probe_injects_requested_count_and_events_into_runner() -> None:
    external = _function(_probe_tree(), "_probe_external")
    runner_calls = [
        node
        for node in ast.walk(external)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "AnsweringRunner"
    ]

    assert (
        len(runner_calls),
        {keyword.arg for keyword in runner_calls[0].keywords},
        "requested_agent_count" in _loaded_names(external),
    ) == (
        1,
        {
            "context_preparer",
            "phases_factory",
            "events",
            "requested_external_agent_count",
        },
        True,
    )


def test_probe_summary_uses_final_result_and_event_progress_not_internal_outcome() -> (
    None
):
    external = _function(_probe_tree(), "_probe_external")
    names = _loaded_names(external)
    text = ast.unparse(external)

    assert (
        "ExternalSearchOutcome" not in names,
        "outcome" not in names,
        "last_outcome" not in text,
        "deduplicated_evidence_count" not in text,
        "effective_agent_count" not in text,
    ) == (True, True, True, True, True)


def test_direct_probe_does_not_construct_external_provider_or_old_search_seam() -> None:
    direct = _function(_probe_tree(), "_probe_direct")
    names = _loaded_names(direct)
    text = ast.unparse(direct)

    assert (
        "build_external_research_runtime_factory" not in names,
        "build_external_search_service" not in names,
        "_UnreachableExternalSearch" not in names,
        "DEEPSEEK_API_KEY" not in text,
        "TAVILY_API_KEY" not in text,
    ) == (True, True, True, True, True)
