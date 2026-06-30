"""GeminiQuestionPlanner tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr, ValidationError

from app.agent.contract import AnswerQuestionInput
from app.agent.planning.ai.gemini import (
    GeminiQuestionPlanner,
    GeminiQuestionPlannerResponseDefect,
    QuestionPlannerResponseInvalidError,
)
from app.agent.planning.ai.gemini_spec import GEMINI_QUESTION_PLANNER_SPEC
from app.agent.planning.plan_draft import QuestionPlanDraft
from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
)
from app.analysis.gemini_error_translator import (
    GeminiContentRejectionReason,
    GeminiStateReason,
)
from app.config import settings


@pytest.fixture(autouse=True)
def _set_gemini_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "gemini_api_key", SecretStr("test-key"))


def _input() -> AnswerQuestionInput:
    return AnswerQuestionInput(
        question="今日のNVIDIAの発表は？",
        as_of=datetime(2026, 6, 29, tzinfo=UTC),
    )


def _stub_response(text: str, *, finish_reason_name: str | None = None) -> MagicMock:
    response = MagicMock()
    response.text = text
    if finish_reason_name is None:
        response.candidates = []
    else:
        candidate = MagicMock()
        candidate.finish_reason = MagicMock(name=finish_reason_name)
        candidate.finish_reason.name = finish_reason_name
        response.candidates = [candidate]
    return response


def _patch_generate_content(
    planner: GeminiQuestionPlanner,
    response: MagicMock,
) -> AsyncMock:
    mock_call = AsyncMock(return_value=response)
    planner._client = MagicMock()
    planner._client.aio.models.generate_content = mock_call
    return mock_call


def test_init_raises_configuration_error_when_api_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "gemini_api_key", SecretStr(""))

    with pytest.raises(AIProviderConfigurationError) as exc_info:
        GeminiQuestionPlanner()

    assert exc_info.value.reason is GeminiStateReason.NOT_CONFIGURED


def test_property_contracts_return_spec_values() -> None:
    planner = GeminiQuestionPlanner()

    assert planner.model_name == GEMINI_QUESTION_PLANNER_SPEC.model
    assert planner.prompt_version == GEMINI_QUESTION_PLANNER_SPEC.version
    assert planner.rate_limit_policy == GEMINI_QUESTION_PLANNER_SPEC.rate_limit_policy


@pytest.mark.asyncio
async def test_call_api_returns_question_plan_draft() -> None:
    planner = GeminiQuestionPlanner()
    payload = {
        "retrieval_mode": "external",
        "internal_queries": [],
        "external_queries": ["NVIDIA latest announcement"],
        "target_time_window": "今日",
        "reason": "今日の発表は外部最新ニュース確認が必要",
    }
    mock_call = _patch_generate_content(planner, _stub_response(json.dumps(payload)))

    plan = await planner.plan(_input())

    assert isinstance(plan, QuestionPlanDraft)
    assert plan.retrieval_mode == "external"
    assert plan.internal_queries == []
    assert plan.external_queries == ["NVIDIA latest announcement"]
    assert plan.target_time_window == "今日"
    assert plan.reason == "今日の発表は外部最新ニュース確認が必要"
    kwargs = mock_call.await_args.kwargs
    assert kwargs["model"] == GEMINI_QUESTION_PLANNER_SPEC.model
    assert "今日のNVIDIAの発表は？" in kwargs["contents"]
    config = kwargs["config"]
    assert config.response_mime_type == "application/json"
    assert isinstance(config.response_schema, dict)
    assert config.response_schema["properties"]["retrieval_mode"]["type"] == "STRING"


@pytest.mark.asyncio
async def test_plan_includes_previous_error_in_repair_prompt() -> None:
    planner = GeminiQuestionPlanner()
    payload = {
        "retrieval_mode": "internal",
        "internal_queries": ["NVIDIA AI GPU 動向"],
        "external_queries": [],
        "reason": "内部記事検索が必要",
    }
    mock_call = _patch_generate_content(planner, _stub_response(json.dumps(payload)))

    await planner.plan(_input(), previous_error="missing field: reason")

    assert "missing field: reason" in mock_call.await_args.kwargs["contents"]


@pytest.mark.asyncio
async def test_invalid_json_raises_response_invalid() -> None:
    planner = GeminiQuestionPlanner()
    _patch_generate_content(planner, _stub_response("not json"))

    with pytest.raises(QuestionPlannerResponseInvalidError) as exc_info:
        await planner.plan(_input())

    assert exc_info.value.defect is GeminiQuestionPlannerResponseDefect.NOT_JSON


@pytest.mark.asyncio
async def test_non_object_payload_raises_response_invalid() -> None:
    planner = GeminiQuestionPlanner()
    _patch_generate_content(planner, _stub_response("[1, 2, 3]"))

    with pytest.raises(QuestionPlannerResponseInvalidError) as exc_info:
        await planner.plan(_input())

    assert exc_info.value.defect is GeminiQuestionPlannerResponseDefect.NOT_OBJECT


@pytest.mark.asyncio
async def test_schema_invalid_payload_raises_validation_error() -> None:
    planner = GeminiQuestionPlanner()
    _patch_generate_content(planner, _stub_response(json.dumps({"retrieval_mode": 1})))

    with pytest.raises(ValidationError):
        await planner.plan(_input())


@pytest.mark.asyncio
async def test_finish_reason_safety_raises_output_blocked() -> None:
    planner = GeminiQuestionPlanner()
    _patch_generate_content(
        planner,
        _stub_response("{}", finish_reason_name="SAFETY"),
    )

    with pytest.raises(AIProviderOutputBlockedError) as exc_info:
        await planner.plan(_input())

    assert exc_info.value.reason is GeminiContentRejectionReason.SAFETY


@pytest.mark.asyncio
async def test_sdk_timeout_translates_to_network_error() -> None:
    planner = GeminiQuestionPlanner()
    mock_call = AsyncMock(side_effect=TimeoutError("deadline"))
    planner._client = MagicMock()
    planner._client.aio.models.generate_content = mock_call

    with pytest.raises(AIProviderNetworkError):
        await planner.plan(_input())
