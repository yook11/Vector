"""Shared answering contract tests."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.agent.question_context.contract import QuestionContext


def _request_type(module_name: str, type_name: str) -> type[object]:
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        pytest.fail(f"{module_name} must define {type_name}: {exc}")
    request_type = getattr(module, type_name, None)
    if request_type is None:
        pytest.fail(f"{module_name} must define {type_name}")
    return request_type


def test_answering_request_is_a_frozen_context_consumer_wrapper() -> None:
    request_type = _request_type("app.agent.answering.contract", "AnsweringRequest")
    context = QuestionContext(standalone_question="NVIDIA の直近発表は？")
    as_of = datetime(2026, 7, 10, tzinfo=UTC)
    request = request_type(context=context, as_of=as_of)

    with pytest.raises(ValidationError):
        request.context = QuestionContext(standalone_question="別の質問")
    with pytest.raises(ValidationError):
        request_type(context=context, as_of=as_of, previous_answer="前の回答")

    assert (
        set(request_type.model_fields),
        request_type.model_fields["context"].annotation,
        request_type.model_fields["as_of"].annotation,
        request.context is context,
        request.context,
        request.as_of,
        "as_of" not in QuestionContext.model_fields,
    ) == (
        {"context", "as_of"},
        QuestionContext,
        datetime,
        True,
        context,
        as_of,
        True,
    )
