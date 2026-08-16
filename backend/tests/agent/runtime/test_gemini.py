"""GeminiAgentRuntime の one-attempt behavior tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from inspect import signature
from types import SimpleNamespace

import pytest
from google.genai import errors as genai_errors

from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError
from app.agent.runtime.gemini import GeminiAgentRuntime
from app.analysis.ai_provider_errors import (
    AIProviderContentRejectionKind,
    AIProviderFailureMode,
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
    AIProviderOutputTruncatedError,
    AIProviderRateLimitedError,
    AIProviderUsageLimitExhaustedError,
)
from app.analysis.gemini_error_translator import GeminiContentRejectionReason
from tests.agent.runtime._helpers import (
    FakeGeminiClient,
    FakeResponse,
    RuntimeOutput,
    ValidationProbeOutput,
    blocked_response,
    finished_response,
    make_agent,
    success_response,
)
from tests.cloudwatch.records import metric_records


@dataclass(frozen=True, slots=True)
class DataclassRuntimeOutput:
    result: str
    tags: list[str]


async def test_constructor_accepts_only_borrowed_async_client() -> None:
    """runtime が借用した非同期 client だけを受け取る境界を守る。"""
    client = FakeGeminiClient([success_response()])

    assert list(signature(GeminiAgentRuntime).parameters) == ["client"]
    GeminiAgentRuntime(client=client)


async def test_call_uses_provider_once_and_returns_validated_output_directly() -> (
    None
):
    """一試行が一度だけ provider を呼び検証済み出力を返す。"""
    client = FakeGeminiClient([success_response(result="validated")])
    runtime = GeminiAgentRuntime(client=client)

    output = await runtime.call(make_agent(), "typed input", attempt_number=1)

    assert output == RuntimeOutput(result="validated", tags=["runtime"])
    assert client.models.generate_content.await_count == 1
    client.close.assert_not_awaited()
    client.aclose.assert_not_awaited()


async def test_call_validates_a_declared_dataclass_output_type() -> None:
    """宣言された dataclass 出力型を runtime 境界で検証する。"""
    client = FakeGeminiClient([success_response(result="dataclass")])
    runtime = GeminiAgentRuntime(client=client)
    agent = replace(make_agent(), output_type=DataclassRuntimeOutput)

    output = await runtime.call(agent, "typed input", attempt_number=1)

    assert output == DataclassRuntimeOutput(result="dataclass", tags=["runtime"])


async def test_request_separates_instructions_contents_and_thaws_schema() -> None:
    """指示・入力・schema を provider request で独立して保持する。"""
    client = FakeGeminiClient([success_response()])
    runtime = GeminiAgentRuntime(client=client)
    instructions = "SYSTEM_INSTRUCTIONS_SENTINEL_77ca"
    contents = "TASK_CONTENTS_SENTINEL_158b"
    agent = make_agent(
        instructions=instructions,
        rendered_input=contents,
        temperature=None,
        max_output_tokens=456,
    )

    await runtime.call(agent, object(), attempt_number=1)

    kwargs = client.models.generate_content.await_args.kwargs
    config = kwargs["config"]
    explicit_config = config.model_dump(exclude_unset=True)
    response_schema = config.response_schema
    assert kwargs["model"] == agent.model.name
    assert kwargs["contents"] == contents
    assert config.system_instruction == instructions
    assert config.response_mime_type == "application/json"
    assert explicit_config["max_output_tokens"] == 456
    assert "temperature" not in explicit_config
    assert isinstance(response_schema, dict)
    assert isinstance(response_schema["required"], list)
    assert isinstance(response_schema["properties"], dict)
    assert isinstance(response_schema["properties"]["tags"], dict)
    assert isinstance(response_schema["properties"]["tags"]["items"], dict)


async def test_same_runtime_does_not_carry_agent_or_input_state_between_calls() -> (
    None
):
    """同一 runtime の連続呼出しで agent と入力の状態を持ち越さない。"""
    client = FakeGeminiClient(
        [
            success_response(result="first", tags=["one"]),
            success_response(result="second", tags=["two"]),
        ]
    )
    runtime = GeminiAgentRuntime(client=client)
    first_agent = make_agent(
        name="first_agent",
        instructions="FIRST_INSTRUCTIONS_SENTINEL",
        rendered_input="FIRST_CONTENTS_SENTINEL",
        model_name="gemini-first-model",
        temperature=0.1,
        max_output_tokens=111,
    )
    second_agent = make_agent(
        name="second_agent",
        instructions="SECOND_INSTRUCTIONS_SENTINEL",
        rendered_input="SECOND_CONTENTS_SENTINEL",
        model_name="gemini-second-model",
        temperature=0.9,
        max_output_tokens=222,
    )

    first_output = await runtime.call(first_agent, "first input", attempt_number=1)
    second_output = await runtime.call(second_agent, "second input", attempt_number=2)

    first_call, second_call = client.models.generate_content.await_args_list
    assert first_output.result == "first"
    assert second_output.result == "second"
    assert first_call.kwargs["model"] == "gemini-first-model"
    assert first_call.kwargs["contents"] == "FIRST_CONTENTS_SENTINEL"
    assert first_call.kwargs["config"].system_instruction == (
        "FIRST_INSTRUCTIONS_SENTINEL"
    )
    assert first_call.kwargs["config"].temperature == 0.1
    assert second_call.kwargs["model"] == "gemini-second-model"
    assert second_call.kwargs["contents"] == "SECOND_CONTENTS_SENTINEL"
    assert second_call.kwargs["config"].system_instruction == (
        "SECOND_INSTRUCTIONS_SENTINEL"
    )
    assert second_call.kwargs["config"].temperature == 0.9


@pytest.mark.parametrize(
    ("response_text", "defect_name"),
    [
        ("MODEL_OUTPUT_SENTINEL_NOT_JSON", "RESPONSE_NOT_JSON"),
        (json.dumps(["MODEL_OUTPUT_SENTINEL_NOT_OBJECT"]), "RESPONSE_NOT_OBJECT"),
    ],
)
async def test_invalid_response_shape_maps_to_provider_neutral_defect(
    response_text: str,
    defect_name: str,
) -> None:
    """不正な応答形を安全な provider 中立 defect に写像する。"""
    client = FakeGeminiClient([FakeResponse(text=response_text)])
    runtime = GeminiAgentRuntime(client=client)

    with pytest.raises(AgentResponseInvalidError) as exc_info:
        await runtime.call(make_agent(), "typed input", attempt_number=1)

    assert exc_info.value.defect is getattr(AgentResponseDefect, defect_name)
    assert "MODEL_OUTPUT_SENTINEL" not in str(exc_info.value)
    assert "MODEL_OUTPUT_SENTINEL" not in (exc_info.value.repair_hint or "")
    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    assert client.models.generate_content.await_count == 1


async def test_output_validation_error_exposes_only_safe_repair_fields() -> None:
    """出力検証エラーから安全な修正情報だけを公開する。"""
    model_output_sentinel = "MODEL_OUTPUT_SENTINEL_SECRET_4e91"
    payload = {
        "score": 0,
        "secret_number": model_output_sentinel,
        "unsafe": "trigger validator",
    }
    client = FakeGeminiClient([FakeResponse(text=json.dumps(payload))])
    runtime = GeminiAgentRuntime(client=client)

    with pytest.raises(AgentResponseInvalidError) as exc_info:
        await runtime.call(
            make_agent(output_type=ValidationProbeOutput),
            "typed input",
            attempt_number=1,
        )

    error = exc_info.value
    safe_error_text = f"{error}\n{error.repair_hint or ''}"
    assert error.defect is AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH
    assert "score" in safe_error_text
    assert "greater_than_equal" in safe_error_text
    assert "ge" in safe_error_text
    assert "secret_number" in safe_error_text
    assert "int_parsing" in safe_error_text
    assert "unsafe" in safe_error_text
    assert "value_error" in safe_error_text
    assert model_output_sentinel not in safe_error_text
    assert "ARBITRARY_CTX_SENTINEL_7c62" not in safe_error_text
    assert "Input should be" not in safe_error_text
    assert "errors.pydantic.dev" not in safe_error_text
    assert error.__context__ is None
    assert error.__cause__ is None


async def test_unknown_extra_field_location_is_collapsed_to_fixed_placeholder() -> None:
    """未知の追加 field 名を固定の安全な表現へ畳み込む。"""
    unknown_field_sentinels = (
        "MODEL_OUTPUT_UNKNOWN_KEY_SENTINEL_4b1e",
        "MODEL_OUTPUT_DIFFERENT_KEY_SENTINEL_9f73",
    )
    client = FakeGeminiClient(
        [
            FakeResponse(
                text=json.dumps(
                    {
                        "result": "accepted",
                        "tags": ["runtime"],
                        sentinel: "unknown extra value",
                    }
                )
            )
            for sentinel in unknown_field_sentinels
        ]
    )
    runtime = GeminiAgentRuntime(client=client)
    errors = []

    for attempt_number in (1, 2):
        with pytest.raises(AgentResponseInvalidError) as exc_info:
            await runtime.call(
                make_agent(),
                "typed input",
                attempt_number=attempt_number,
            )
        errors.append(exc_info.value)

    safe_error_text = "\n".join(str(error) for error in errors)
    assert all(
        error.defect is AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH for error in errors
    )
    assert errors[0].repair_hint
    assert errors[0].repair_hint == errors[1].repair_hint
    assert "extra_forbidden" in errors[0].repair_hint
    assert all(sentinel not in safe_error_text for sentinel in unknown_field_sentinels)


@pytest.mark.parametrize(
    ("finish_reason", "expected_reason", "expected_kind_name"),
    [
        ("SAFETY", GeminiContentRejectionReason.SAFETY, "SAFETY"),
        ("RECITATION", GeminiContentRejectionReason.RECITATION, "OTHER"),
        ("BLOCKLIST", GeminiContentRejectionReason.BLOCKLIST, "OTHER"),
        (
            "PROHIBITED_CONTENT",
            GeminiContentRejectionReason.PROHIBITED_CONTENT,
            "OTHER",
        ),
        ("SPII", GeminiContentRejectionReason.SPII, "OTHER"),
    ],
)
async def test_blocked_finish_reason_maps_to_existing_provider_error(
    finish_reason: str,
    expected_reason: GeminiContentRejectionReason,
    expected_kind_name: str,
) -> None:
    """拒否理由を既存の provider エラー語彙へ対応付ける。"""
    client = FakeGeminiClient([blocked_response(finish_reason)])
    runtime = GeminiAgentRuntime(client=client)

    with pytest.raises(AIProviderOutputBlockedError) as exc_info:
        await runtime.call(make_agent(), "typed input", attempt_number=1)

    assert exc_info.value.reason is expected_reason
    assert exc_info.value.rejection_kind is getattr(
        AIProviderContentRejectionKind,
        expected_kind_name,
    )
    assert client.models.generate_content.await_count == 1
    client.close.assert_not_awaited()
    client.aclose.assert_not_awaited()


async def test_call_max_tokens_finish_reason_is_truncated_even_with_valid_json() -> (
    None
):
    """R1条件1・2: MAX_TOKENSはJSONがschemaに完全に妥当でも打ち切りとして分類される。

    finish_reasonの分類をJSONの偶然の妥当性より優先させる(仕様が撤去した
    「JSONが閉じないことによる偶然の検出への依存」をnon-stream経路でも解消する)。
    """
    client = FakeGeminiClient([finished_response("MAX_TOKENS")])
    runtime = GeminiAgentRuntime(client=client)

    with pytest.raises(AIProviderOutputTruncatedError) as exc_info:
        await runtime.call(make_agent(), "typed input", attempt_number=1)

    assert exc_info.value.CODE == "ai_error_output_truncated"
    assert exc_info.value.FAILURE_MODE is AIProviderFailureMode.ATTEMPT_SCOPED
    assert exc_info.value.reason is not None
    assert exc_info.value.reason.value == "output_token_limit_reached"
    assert client.models.generate_content.await_count == 1
    client.close.assert_not_awaited()
    client.aclose.assert_not_awaited()


async def test_call_stop_finish_reason_still_succeeds() -> None:
    """R1条件3(回帰ガード): finish_reason=STOPは現行どおり正常終了する。"""
    client = FakeGeminiClient([finished_response("STOP", result="stopped")])
    runtime = GeminiAgentRuntime(client=client)

    output = await runtime.call(make_agent(), "typed input", attempt_number=1)

    assert output == RuntimeOutput(result="stopped", tags=["runtime"])


async def test_non_stream_prompt_feedback_precedes_candidate_safety() -> None:
    response = SimpleNamespace(
        text='{"result":"must not parse","tags":[]}',
        candidates=[
            SimpleNamespace(
                finish_reason=SimpleNamespace(name="SAFETY"),
            )
        ],
        usage_metadata=None,
        prompt_feedback=SimpleNamespace(block_reason="SAFETY"),
    )
    client = FakeGeminiClient([response])
    runtime = GeminiAgentRuntime(client=client)

    with pytest.raises(AIProviderInputRejectedError) as exc_info:
        await runtime.call(make_agent(), "typed input", attempt_number=1)

    assert exc_info.value.reason is GeminiContentRejectionReason.INPUT_BLOCKED
    assert exc_info.value.rejection_kind is AIProviderContentRejectionKind.SAFETY
    assert exc_info.value.is_safety_rejection is True
    assert client.models.generate_content.await_count == 1


async def test_known_gemini_failure_uses_existing_error_translation() -> None:
    """既知の Gemini 障害を既存のアプリケーション例外へ翻訳する。"""
    client = FakeGeminiClient([TimeoutError("PROVIDER_SENTINEL_TIMEOUT_79ab")])
    runtime = GeminiAgentRuntime(client=client)

    with pytest.raises(AIProviderNetworkError):
        await runtime.call(make_agent(), "typed input", attempt_number=1)

    assert client.models.generate_content.await_count == 1
    client.close.assert_not_awaited()
    client.aclose.assert_not_awaited()


async def test_unclassified_exception_propagates_with_identity() -> None:
    """未分類例外の同一性を失わずに伝播させる。"""
    error = RuntimeError("UNCLASSIFIED_EXCEPTION_SENTINEL_2c5e")
    client = FakeGeminiClient([error])
    runtime = GeminiAgentRuntime(client=client)

    with pytest.raises(RuntimeError) as exc_info:
        await runtime.call(make_agent(), "typed input", attempt_number=1)

    assert exc_info.value is error
    assert client.models.generate_content.await_count == 1
    client.close.assert_not_awaited()
    client.aclose.assert_not_awaited()


@pytest.mark.parametrize("attempt_number", [0, -1])
async def test_non_positive_attempt_number_is_rejected_before_provider_call(
    attempt_number: int,
) -> None:
    """非正の試行番号では provider 呼出し前に拒否する。"""
    client = FakeGeminiClient([success_response()])
    runtime = GeminiAgentRuntime(client=client)

    with pytest.raises(ValueError):
        await runtime.call(
            make_agent(),
            "typed input",
            attempt_number=attempt_number,
        )

    client.models.generate_content.assert_not_awaited()


async def test_renderer_failure_propagates_without_provider_call() -> None:
    """入力描画の失敗時に provider を呼び出さない。"""
    error = RuntimeError("RENDERER_FAILURE_SENTINEL_93b0")
    client = FakeGeminiClient([success_response()])
    agent = make_agent()
    agent = replace(
        agent,
        prompt=type(agent.prompt)(
            version=agent.prompt.version,
            instructions=agent.prompt.instructions,
            input_renderer=lambda _input: (_ for _ in ()).throw(error),
        ),
    )
    runtime = GeminiAgentRuntime(client=client)

    with pytest.raises(RuntimeError) as exc_info:
        await runtime.call(agent, "typed input", attempt_number=1)

    assert exc_info.value is error
    client.models.generate_content.assert_not_awaited()


async def test_config_construction_failure_happens_before_provider_call() -> None:
    """request config の不備を provider 呼出し前に表面化させる。"""
    client = FakeGeminiClient([success_response()])
    agent = make_agent()
    agent = replace(
        agent,
        model_settings=type(agent.model_settings)(
            temperature=SimpleNamespace(invalid="temperature"),
            max_output_tokens=321,
        ),
    )
    runtime = GeminiAgentRuntime(client=client)

    with pytest.raises((TypeError, ValueError)):
        await runtime.call(agent, "typed input", attempt_number=1)

    client.models.generate_content.assert_not_awaited()


# ai_provider_exhausted EMF emit (CloudWatch A6) — 枯渇系翻訳のみが打点する


_EXHAUSTED_METRIC = "ai_provider_exhausted"


def _quota_exhausted_sdk_error() -> genai_errors.ClientError:
    """429 + per-day QuotaFailure details の実レスポンス形 (枯渇に翻訳される)。"""
    response_json = {
        "error": {
            "status": "RESOURCE_EXHAUSTED",
            "message": "You exceeded your current quota, check your plan and billing.",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [
                        {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}
                    ],
                }
            ],
        }
    }
    return genai_errors.ClientError(429, response_json)


def _rate_limited_sdk_error() -> genai_errors.ClientError:
    """429 だが details 無し (per-minute バースト等、rate limit に翻訳される)。"""
    response_json = {
        "error": {"status": "RESOURCE_EXHAUSTED", "message": "rate limit reached"}
    }
    return genai_errors.ClientError(429, response_json)


async def test_call_quota_exhausted_sdk_error_emits_ai_provider_exhausted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """quota 超過 (usage_limit_exhausted 翻訳) は provider=gemini で emit する。"""
    client = FakeGeminiClient([_quota_exhausted_sdk_error()])
    runtime = GeminiAgentRuntime(client=client)

    with pytest.raises(AIProviderUsageLimitExhaustedError):
        await runtime.call(make_agent(), "typed input", attempt_number=1)

    records = metric_records(capsys.readouterr().out, _EXHAUSTED_METRIC)
    assert len(records) == 1
    assert records[0]["kind"] == AIProviderUsageLimitExhaustedError.CODE
    assert records[0]["provider"] == "gemini"


async def test_call_plain_rate_limited_sdk_error_does_not_emit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """一時的 rate limit (時間経過で回復) は枯渇ではないため emit しない。"""
    client = FakeGeminiClient([_rate_limited_sdk_error()])
    runtime = GeminiAgentRuntime(client=client)

    with pytest.raises(AIProviderRateLimitedError):
        await runtime.call(make_agent(), "typed input", attempt_number=1)

    assert metric_records(capsys.readouterr().out, _EXHAUSTED_METRIC) == []
