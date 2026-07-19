"""Question-answering probe のexternal runtime wiring構造を固定する。"""

from __future__ import annotations

import ast
from pathlib import Path


def test_probe_uses_answering_runner_phases_and_composition_service_builder() -> None:
    probe_path = (
        Path(__file__).resolve().parents[2] / "scripts" / "probe_question_answering.py"
    )
    tree = ast.parse(probe_path.read_text(encoding="utf-8"))
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    loaded_names = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    legacy_boundaries = {
        "AnswerQuestionInput",
        "QuestionAnsweringAgent",
        "QuestionAnsweringOrchestrator",
        "build_question_answering_starting_agent",
        "build_question_answering_agent",
        "starting_agent",
    }

    assert (
        {
            "AnsweringPhases",
            "AnsweringRunner",
            "RunContext",
            "RunInput",
            "build_external_search_service",
        }
        <= imported_names,
        {
            "AnsweringPhases",
            "AnsweringRunner",
            "RunContext",
            "RunInput",
            "build_external_search_service",
        }
        <= called_names,
        legacy_boundaries.isdisjoint(imported_names),
        legacy_boundaries.isdisjoint(loaded_names),
        {
            "AsyncOpenAI",
            "DeepSeekAgentRuntime",
            "ExternalSearchResearchRunner",
            "TavilyExternalSearchTool",
            "make_safe_async_client",
        }.isdisjoint(imported_names),
    ) == (True, True, True, True, True)
