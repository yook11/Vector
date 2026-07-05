"""DeepSeek evidence selector adapter tests."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from openai import APIConnectionError, APITimeoutError
from openai import RateLimitError as OpenAIRateLimitError
from pydantic import SecretStr

from app.agent.external_search import ExternalEvidenceSelectorError
from app.agent.external_search.ai.spec import (
    DEEPSEEK_EVIDENCE_SELECTOR_SPEC,
    EXTERNAL_DEEPSEEK_TIMEOUT_SECONDS,
)
from app.agent.external_search.contract import (
    EVIDENCE_CLAIM_MAX_CHARS,
    EXTERNAL_SEARCH_MISSING_LIMIT_PER_TASK,
    MISSING_ITEM_MAX_CHARS,
)
from app.analysis.ai_provider_errors import AIProviderConfigurationError
from app.analysis.deepseek_error_translator import DeepSeekStateReason
from app.config import settings
from tests.agent.external_search.ai.helpers import (
    DEEPSEEK_AS_OF,
    candidate,
    make_request,
    make_response,
    make_status_error,
    patch_adapter_call,
    stub_response,
    task,
)


@pytest.mark.asyncio
async def test_select_builds_result_through_from_raw() -> None:
    from app.agent.external_search.ai.deepseek import DeepSeekEvidenceSelector

    selector = DeepSeekEvidenceSelector()
    long_claim = "あ" * (EVIDENCE_CLAIM_MAX_CHARS + 1)
    patch_adapter_call(
        selector,
        response=stub_response(
            tool_name=DEEPSEEK_EVIDENCE_SELECTOR_SPEC.tool_name,
            arguments=json.dumps(
                {
                    "selections": [
                        {
                            "candidate_index": 0,
                            "claim": long_claim,
                            "why_selected": "公式発表に近い候補だから",
                        }
                    ],
                    "missing": [],
                }
            ),
        ),
    )

    result = await selector.select(
        task=task(),
        candidates=[candidate()],
        as_of=DEEPSEEK_AS_OF,
    )

    assert len(result.selections) == 1
    assert result.selections[0].claim == long_claim[:EVIDENCE_CLAIM_MAX_CHARS]


@pytest.mark.asyncio
async def test_select_clamps_missing_through_from_raw() -> None:
    from app.agent.external_search.ai.deepseek import DeepSeekEvidenceSelector

    selector = DeepSeekEvidenceSelector()
    long_missing = "未" * (MISSING_ITEM_MAX_CHARS + 1)
    patch_adapter_call(
        selector,
        response=stub_response(
            tool_name=DEEPSEEK_EVIDENCE_SELECTOR_SPEC.tool_name,
            arguments=json.dumps(
                {
                    "selections": [],
                    "missing": [long_missing]
                    * (EXTERNAL_SEARCH_MISSING_LIMIT_PER_TASK + 1),
                }
            ),
        ),
    )

    result = await selector.select(
        task=task(),
        candidates=[candidate()],
        as_of=DEEPSEEK_AS_OF,
    )

    assert len(result.missing) == EXTERNAL_SEARCH_MISSING_LIMIT_PER_TASK
    assert result.missing[0] == long_missing[:MISSING_ITEM_MAX_CHARS]


@pytest.mark.asyncio
async def test_select_accepts_empty_selections() -> None:
    from app.agent.external_search.ai.deepseek import DeepSeekEvidenceSelector

    selector = DeepSeekEvidenceSelector()
    patch_adapter_call(
        selector,
        response=stub_response(
            tool_name=DEEPSEEK_EVIDENCE_SELECTOR_SPEC.tool_name,
            arguments=json.dumps({"selections": [], "missing": []}),
        ),
    )

    result = await selector.select(
        task=task(),
        candidates=[],
        as_of=DEEPSEEK_AS_OF,
    )

    assert result.selections == []
    assert result.missing == []


@pytest.mark.asyncio
async def test_select_sends_structured_output_mechanism_to_sdk() -> None:
    from app.agent.external_search.ai.deepseek import DeepSeekEvidenceSelector

    selector = DeepSeekEvidenceSelector()
    mock_call = patch_adapter_call(
        selector,
        response=stub_response(
            tool_name=DEEPSEEK_EVIDENCE_SELECTOR_SPEC.tool_name,
            arguments=json.dumps({"selections": [], "missing": []}),
        ),
    )

    await selector.select(
        task=task("SDK_PROMPT_MARKER"),
        candidates=[candidate("CANDIDATE_SDK_MARKER")],
        as_of=DEEPSEEK_AS_OF,
    )

    kwargs = mock_call.await_args.kwargs
    prompt = kwargs["messages"][0]["content"]
    assert kwargs["model"] == DEEPSEEK_EVIDENCE_SELECTOR_SPEC.model
    assert kwargs["messages"][0]["role"] == "user"
    assert "SDK_PROMPT_MARKER" in prompt
    assert "CANDIDATE_SDK_MARKER" in prompt
    assert "SHOULD_NOT_APPEAR" not in prompt
    assert kwargs["tools"][0]["function"]["name"] == (
        DEEPSEEK_EVIDENCE_SELECTOR_SPEC.tool_name
    )
    assert kwargs["tools"][0]["function"]["strict"] is True
    assert kwargs["tools"][0]["function"]["parameters"] == dict(
        DEEPSEEK_EVIDENCE_SELECTOR_SPEC.response_schema
    )
    assert (
        kwargs["tool_choice"]["function"]["name"]
        == DEEPSEEK_EVIDENCE_SELECTOR_SPEC.tool_name
    )
    assert kwargs["extra_body"]["thinking"]["type"] == "disabled"
    assert (
        kwargs["max_tokens"] == DEEPSEEK_EVIDENCE_SELECTOR_SPEC.gen_config["max_tokens"]
    )


@pytest.mark.parametrize(
    "response,expected_reason",
    [
        (
            stub_response(
                arguments=None,
                tool_name=DEEPSEEK_EVIDENCE_SELECTOR_SPEC.tool_name,
                no_tool_calls=True,
            ),
            "external_search_deepseek_no_tool_call",
        ),
        (
            stub_response(arguments="{}", tool_name="wrong_tool"),
            "external_search_deepseek_wrong_tool_name",
        ),
        (
            stub_response(
                arguments="RAW_RESPONSE_MARKER not json",
                tool_name=DEEPSEEK_EVIDENCE_SELECTOR_SPEC.tool_name,
            ),
            "external_search_deepseek_arguments_not_json",
        ),
        (
            stub_response(
                arguments="[1, 2, 3]",
                tool_name=DEEPSEEK_EVIDENCE_SELECTOR_SPEC.tool_name,
            ),
            "external_search_deepseek_arguments_not_dict",
        ),
        (
            stub_response(
                arguments=json.dumps({"missing": []}),
                tool_name=DEEPSEEK_EVIDENCE_SELECTOR_SPEC.tool_name,
            ),
            "external_search_deepseek_arguments_schema_invalid",
        ),
        (
            stub_response(
                arguments=json.dumps({"selections": [], "missing": "none"}),
                tool_name=DEEPSEEK_EVIDENCE_SELECTOR_SPEC.tool_name,
            ),
            "external_search_deepseek_arguments_schema_invalid",
        ),
        (
            stub_response(
                arguments=json.dumps({"selections": [123], "missing": []}),
                tool_name=DEEPSEEK_EVIDENCE_SELECTOR_SPEC.tool_name,
            ),
            "external_search_deepseek_arguments_schema_invalid",
        ),
        (
            stub_response(
                arguments=json.dumps(
                    {
                        "selections": [
                            {
                                "candidate_index": -1,
                                "claim": "根拠",
                                "why_selected": "理由",
                            }
                        ],
                        "missing": [],
                    }
                ),
                tool_name=DEEPSEEK_EVIDENCE_SELECTOR_SPEC.tool_name,
            ),
            "external_search_deepseek_arguments_schema_invalid",
        ),
    ],
)
@pytest.mark.asyncio
async def test_select_wraps_envelope_defects_with_reason(
    response: MagicMock,
    expected_reason: str,
) -> None:
    from app.agent.external_search.ai.deepseek import DeepSeekEvidenceSelector

    selector = DeepSeekEvidenceSelector()
    patch_adapter_call(selector, response=response)

    with pytest.raises(ExternalEvidenceSelectorError) as exc_info:
        await selector.select(
            task=task("PROMPT_MARKER"),
            candidates=[candidate("CANDIDATE_PROMPT_MARKER")],
            as_of=DEEPSEEK_AS_OF,
        )

    assert exc_info.value.reason == expected_reason
    assert str(exc_info.value) == expected_reason
    assert "PROMPT_MARKER" not in str(exc_info.value)
    assert "CANDIDATE_PROMPT_MARKER" not in str(exc_info.value)
    assert "RAW_RESPONSE_MARKER" not in str(exc_info.value)
    assert "SECRET_API_KEY" not in str(exc_info.value)


@pytest.mark.parametrize(
    "side_effect,expected_reason",
    [
        (
            APITimeoutError(request=make_request()),
            DeepSeekStateReason.TIMEOUT,
        ),
        (
            APIConnectionError(request=make_request()),
            DeepSeekStateReason.CONNECTION,
        ),
        (
            OpenAIRateLimitError(
                "RAW_RESPONSE_MARKER rate",
                response=make_response(429),
                body=None,
            ),
            DeepSeekStateReason.RATE_LIMITED,
        ),
        (
            make_status_error(402, "RAW_RESPONSE_MARKER insufficient"),
            DeepSeekStateReason.INSUFFICIENT_BALANCE,
        ),
    ],
)
@pytest.mark.asyncio
async def test_select_wraps_sdk_errors_without_leaking_sdk_type_or_message(
    side_effect: Exception,
    expected_reason: DeepSeekStateReason,
) -> None:
    from app.agent.external_search.ai.deepseek import DeepSeekEvidenceSelector

    selector = DeepSeekEvidenceSelector()
    patch_adapter_call(selector, side_effect=side_effect)

    with pytest.raises(ExternalEvidenceSelectorError) as exc_info:
        await selector.select(
            task=task("PROMPT_MARKER"),
            candidates=[candidate("CANDIDATE_PROMPT_MARKER")],
            as_of=DEEPSEEK_AS_OF,
        )

    assert exc_info.value.reason == expected_reason
    assert "RAW_RESPONSE_MARKER" not in str(exc_info.value)
    assert "PROMPT_MARKER" not in str(exc_info.value)
    assert "CANDIDATE_PROMPT_MARKER" not in str(exc_info.value)
    assert "SECRET_API_KEY" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_select_propagates_untranslated_exceptions() -> None:
    from app.agent.external_search.ai.deepseek import DeepSeekEvidenceSelector

    selector = DeepSeekEvidenceSelector()
    original = ValueError("untranslated failure")
    patch_adapter_call(selector, side_effect=original)

    with pytest.raises(ValueError, match="untranslated failure"):
        await selector.select(
            task=task(),
            candidates=[candidate()],
            as_of=DEEPSEEK_AS_OF,
        )


def test_empty_key_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent.external_search.ai.deepseek import DeepSeekEvidenceSelector

    monkeypatch.setattr(settings, "deepseek_api_key", SecretStr(""))

    with pytest.raises(AIProviderConfigurationError) as exc_info:
        DeepSeekEvidenceSelector()

    assert exc_info.value.reason is DeepSeekStateReason.NOT_CONFIGURED


def test_client_construction_receives_timeout_and_does_not_leak_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.agent.external_search.ai import deepseek as deepseek_module

    calls: list[dict[str, object]] = []

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(settings, "deepseek_api_key", SecretStr("SECRET_API_KEY"))
    monkeypatch.setattr(deepseek_module, "AsyncOpenAI", FakeAsyncOpenAI)

    deepseek_module.DeepSeekEvidenceSelector()

    assert calls == [
        {
            "api_key": "SECRET_API_KEY",
            "base_url": DEEPSEEK_EVIDENCE_SELECTOR_SPEC.base_url,
            "timeout": EXTERNAL_DEEPSEEK_TIMEOUT_SECONDS,
        }
    ]
