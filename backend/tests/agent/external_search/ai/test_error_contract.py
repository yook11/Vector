"""External search LLM port error contract tests."""

from __future__ import annotations

from app.agent.external_search import (
    ExternalEvidenceSelectorError,
    ExternalQueryGenerationError,
)


def test_query_generation_error_keeps_plain_exception_compatibility() -> None:
    error = ExternalQueryGenerationError("query failed")

    assert str(error) == "query failed"
    assert error.reason is None


def test_query_generation_error_exposes_optional_reason_without_message_leak() -> None:
    error = ExternalQueryGenerationError(reason="arguments_schema_invalid")

    assert str(error) == "arguments_schema_invalid"
    assert error.reason == "arguments_schema_invalid"


def test_evidence_selector_error_keeps_plain_exception_compatibility() -> None:
    error = ExternalEvidenceSelectorError("selector failed")

    assert str(error) == "selector failed"
    assert error.reason is None


def test_evidence_selector_error_exposes_optional_reason_without_message_leak() -> None:
    error = ExternalEvidenceSelectorError(reason="no_tool_call")

    assert str(error) == "no_tool_call"
    assert error.reason == "no_tool_call"
