"""Question-answering probe のexternal runtime wiring構造を固定する。"""

from __future__ import annotations

import ast
from pathlib import Path


def test_probe_routes_external_research_through_composition_service_builder() -> None:
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

    assert (
        "build_external_search_service" in imported_names,
        "build_external_search_service" in called_names,
        {
            "AsyncOpenAI",
            "DeepSeekAgentRuntime",
            "ExternalSearchResearchRunner",
            "TavilyExternalSearchTool",
            "make_safe_async_client",
        }.isdisjoint(imported_names),
    ) == (True, True, True)
