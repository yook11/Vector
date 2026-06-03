"""``DeepSeekAssessor._call_api`` の integration テスト。

PR3 で次の流れに rewrite された:
- SDK レスポンスの ``tool_call.arguments`` を ``json.loads`` で dict 化
- ``parse_assessment`` でドメイン型 (``InScope`` / ``OutOfScope``) に詰め替え
- raw 情報と共に ``AssessmentCall`` envelope に格納

検証:
- 正常系: in-scope / out-of-scope の round-trip と envelope field 値
- tool_call 欠落 / wrong tool name → ``AssessmentResponseInvalidError``
  (provider terminal-skip ではなく recoverable)
- arguments JSON 不正 → ``AssessmentResponseInvalidError``
- arguments が dict でない → ``AssessmentResponseInvalidError``
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from app.analysis.assessment.ai.deepseek import DeepSeekAssessor
from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.ai.spec import DEEPSEEK_ASSESSMENT_SPEC
from app.analysis.assessment.domain.result import InScope, InScopeCategory, OutOfScope
from app.analysis.assessment.errors import AssessmentResponseInvalidError
from app.config import settings


@pytest.fixture(autouse=True)
def _set_deepseek_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deepseek_api_key", SecretStr("test-key"))


def _stub_response(
    *,
    arguments: str | None,
    tool_name: str = DEEPSEEK_ASSESSMENT_SPEC.tool_name,
    finish_reason: str = "tool_calls",
    no_tool_calls: bool = False,
) -> MagicMock:
    """SDK Response の最小 stub (choices[0].message.tool_calls[0].function を持つ)。"""
    response = MagicMock()
    choice = MagicMock()
    choice.finish_reason = finish_reason

    if no_tool_calls:
        choice.message.tool_calls = None
    else:
        tool_call = MagicMock()
        tool_call.function = MagicMock()
        tool_call.function.name = tool_name
        tool_call.function.arguments = arguments or ""
        choice.message.tool_calls = [tool_call]

    response.choices = [choice]
    return response


def _patch_assessor_call(assessor: DeepSeekAssessor, response: MagicMock) -> AsyncMock:
    mock_call = AsyncMock(return_value=response)
    assessor._client = MagicMock()
    assessor._client.chat.completions.create = mock_call
    return mock_call


# Round trip: in-scope / out-of-scope


class TestDeepSeekCallApiSuccess:
    @pytest.mark.asyncio
    async def test_in_scope_round_trip(self) -> None:
        assessor = DeepSeekAssessor()
        args = json.dumps(
            {
                "category": "ai",
                "investor_take": "Significant traction.",
                "events": [],
            }
        )
        _patch_assessor_call(assessor, _stub_response(arguments=args))

        call = await assessor._call_api("prompt")

        assert isinstance(call, AssessmentCall)
        assert isinstance(call.result, InScope)
        assert call.result.category == InScopeCategory.AI
        assert call.raw_response == args
        assert call.raw_category == "ai"
        assert call.prompt_version == DEEPSEEK_ASSESSMENT_SPEC.version
        assert call.model_name == DEEPSEEK_ASSESSMENT_SPEC.model

    @pytest.mark.asyncio
    async def test_out_of_scope_round_trip(self) -> None:
        assessor = DeepSeekAssessor()
        args = json.dumps(
            {
                "category": "out_of_scope",
                "investor_take": "Not relevant.",
                "events": [],
            }
        )
        _patch_assessor_call(assessor, _stub_response(arguments=args))

        call = await assessor._call_api("prompt")

        assert isinstance(call.result, OutOfScope)
        assert call.raw_category == "out_of_scope"
        assert call.model_name == DEEPSEEK_ASSESSMENT_SPEC.model


# tool_call 欠落 / wrong name → AssessmentResponseInvalidError (recoverable)


class TestDeepSeekToolCallStructure:
    """tool_call 構造違反は AssessmentResponseInvalidError で raise する。

    AIProviderRequestInvalidError (terminal-skip) で raise しないのは、
    provider は応答したが構造が違っただけ → モデル一時的な揺らぎを「リトライ
    無駄」扱いにしないため (recoverable で cron 救済対象)。
    """

    @pytest.mark.asyncio
    async def test_no_tool_call_raises_response_invalid(self) -> None:
        """Phase 4: 旧 message 検査は廃止 (__str__ は code 固定値のみ)。
        marker class + 固定 code で identity を pin する。"""
        assessor = DeepSeekAssessor()
        _patch_assessor_call(
            assessor,
            _stub_response(arguments=None, no_tool_calls=True, finish_reason="stop"),
        )

        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            await assessor._call_api("prompt")

        assert exc_info.value.code == "assessment_response_invalid"

    @pytest.mark.asyncio
    async def test_wrong_tool_name_raises_response_invalid(self) -> None:
        assessor = DeepSeekAssessor()
        _patch_assessor_call(
            assessor,
            _stub_response(arguments="{}", tool_name="some_other_tool"),
        )

        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            await assessor._call_api("prompt")

        assert exc_info.value.code == "assessment_response_invalid"


# arguments 不正 payload 経路


class TestDeepSeekInvalidArguments:
    @pytest.mark.asyncio
    async def test_invalid_arguments_json_raises_response_invalid(self) -> None:
        """Phase 4: marker class + 固定 code で identity を pin する
        (__str__ に payload を載せない契約)。"""
        assessor = DeepSeekAssessor()
        _patch_assessor_call(assessor, _stub_response(arguments="not json at all"))

        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            await assessor._call_api("prompt")

        assert exc_info.value.code == "assessment_response_invalid"

    @pytest.mark.asyncio
    async def test_non_object_arguments_raises_response_invalid(self) -> None:
        assessor = DeepSeekAssessor()
        _patch_assessor_call(assessor, _stub_response(arguments="[1, 2, 3]"))

        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            await assessor._call_api("prompt")

        assert exc_info.value.code == "assessment_response_invalid"

    @pytest.mark.asyncio
    async def test_missing_key_arguments_raises_response_invalid(self) -> None:
        """parse_assessment の key 欠落で AssessmentResponseInvalidError raise。"""
        assessor = DeepSeekAssessor()
        args = json.dumps({"category": "ai"})  # investor_take 欠落
        _patch_assessor_call(assessor, _stub_response(arguments=args))

        with pytest.raises(AssessmentResponseInvalidError):
            await assessor._call_api("prompt")

    @pytest.mark.asyncio
    async def test_empty_arguments_raises_response_invalid(self) -> None:
        """arguments が空文字 → JSON parse 失敗 → invalid。"""
        assessor = DeepSeekAssessor()
        _patch_assessor_call(assessor, _stub_response(arguments=""))

        with pytest.raises(AssessmentResponseInvalidError):
            await assessor._call_api("prompt")
