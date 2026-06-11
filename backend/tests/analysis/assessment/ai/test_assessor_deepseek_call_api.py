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
from structlog.testing import capture_logs

from app.analysis.assessment.ai.deepseek import (
    DeepSeekAssessor,
    DeepSeekResponseDefect,
)
from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.ai.parse import AssessmentResponseDefect
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
    completion_tokens: int | None = None,
) -> MagicMock:
    """SDK Response の最小 stub (choices[0].message.tool_calls[0].function を持つ)。

    completion_tokens を None のままにすると resp.usage は None になる。
    truncation 観測ログの検証など completion_tokens を具体値で assert したい場合は
    整数値を渡すこと。
    """
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

    if completion_tokens is not None:
        response.usage = MagicMock()
        response.usage.completion_tokens = completion_tokens
    else:
        response.usage = None

    return response


def _patch_assessor_call(assessor: DeepSeekAssessor, response: MagicMock) -> AsyncMock:
    mock_call = AsyncMock(return_value=response)
    assessor._client = MagicMock()
    assessor._client.chat.completions.create = mock_call
    return mock_call


class TestDeepSeekCallApiSuccess:
    @pytest.mark.asyncio
    async def test_in_scope_round_trip(self) -> None:
        assessor = DeepSeekAssessor()
        args = json.dumps(
            {
                "category": "ai",
                "investor_take": "Significant traction.",
                "key_points": [],
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
                "key_points": [],
            }
        )
        _patch_assessor_call(assessor, _stub_response(arguments=args))

        call = await assessor._call_api("prompt")

        assert isinstance(call.result, OutOfScope)
        assert call.raw_category == "out_of_scope"
        assert call.model_name == DEEPSEEK_ASSESSMENT_SPEC.model

    @pytest.mark.asyncio
    async def test_structured_output_mechanism_reaches_sdk(self) -> None:
        """機構 (forced tool_choice + thinking 無効) を structured_output に分離後も
        tuning (max_tokens) と共に create kwargs に届くこと。"""
        assessor = DeepSeekAssessor()
        args = json.dumps({"category": "ai", "investor_take": "x", "key_points": []})
        mock_call = _patch_assessor_call(assessor, _stub_response(arguments=args))

        await assessor._call_api("prompt")

        kwargs = mock_call.await_args.kwargs
        assert (
            kwargs["tool_choice"]["function"]["name"]
            == DEEPSEEK_ASSESSMENT_SPEC.tool_name
        )
        assert kwargs["extra_body"]["thinking"]["type"] == "disabled"
        assert kwargs["max_tokens"] == 512


# tool_call 欠落 / wrong name → AssessmentResponseInvalidError (recoverable)


class TestDeepSeekToolCallStructure:
    """tool_call 構造違反は AssessmentResponseInvalidError で raise する。

    AIProviderRequestInvalidError (terminal-skip) で raise しないのは、
    provider は応答したが構造が違っただけ → モデル一時的な揺らぎを「リトライ
    無駄」扱いにしないため (recoverable で cron 救済対象)。
    """

    @pytest.mark.asyncio
    async def test_no_tool_call_raises_deepseek_no_tool_call(self) -> None:
        """tool_call 欠落は adapter 所有 ``NO_TOOL_CALL`` defect を焼く。

        spec は tool_choice で呼び出しを強制しているため、欠落は provider が機構
        契約を破った状態 = code (語彙) で可視化する (retryability は recoverable 維持)。
        """
        assessor = DeepSeekAssessor()
        _patch_assessor_call(
            assessor,
            _stub_response(arguments=None, no_tool_calls=True, finish_reason="stop"),
        )

        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            await assessor._call_api("prompt")

        assert exc_info.value.code == DeepSeekResponseDefect.NO_TOOL_CALL

    @pytest.mark.asyncio
    async def test_wrong_tool_name_raises_deepseek_wrong_tool_name(self) -> None:
        assessor = DeepSeekAssessor()
        _patch_assessor_call(
            assessor,
            _stub_response(arguments="{}", tool_name="some_other_tool"),
        )

        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            await assessor._call_api("prompt")

        assert exc_info.value.code == DeepSeekResponseDefect.WRONG_TOOL_NAME


# arguments 不正 payload 経路


class TestDeepSeekInvalidArguments:
    @pytest.mark.asyncio
    async def test_invalid_arguments_json_raises_deepseek_arguments_not_json(
        self,
    ) -> None:
        """arguments が非 JSON → adapter 所有 ``ARGUMENTS_NOT_JSON`` defect。"""
        assessor = DeepSeekAssessor()
        _patch_assessor_call(assessor, _stub_response(arguments="not json at all"))

        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            await assessor._call_api("prompt")

        assert exc_info.value.code == DeepSeekResponseDefect.ARGUMENTS_NOT_JSON

    @pytest.mark.asyncio
    async def test_non_object_arguments_raises_deepseek_arguments_not_dict(
        self,
    ) -> None:
        assessor = DeepSeekAssessor()
        _patch_assessor_call(assessor, _stub_response(arguments="[1, 2, 3]"))

        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            await assessor._call_api("prompt")

        assert exc_info.value.code == DeepSeekResponseDefect.ARGUMENTS_NOT_DICT

    @pytest.mark.asyncio
    async def test_missing_key_arguments_surfaces_parse_defect(self) -> None:
        """parse の内容違反 (key 欠落) が adapter を素通りして焼かれる。"""
        assessor = DeepSeekAssessor()
        args = json.dumps({"category": "ai"})  # investor_take 欠落
        _patch_assessor_call(assessor, _stub_response(arguments=args))

        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            await assessor._call_api("prompt")

        assert exc_info.value.code == AssessmentResponseDefect.INVESTOR_TAKE_KEY_MISSING

    @pytest.mark.asyncio
    async def test_empty_arguments_raises_deepseek_arguments_not_json(self) -> None:
        """arguments が空文字 → JSON parse 失敗 → ``ARGUMENTS_NOT_JSON``。"""
        assessor = DeepSeekAssessor()
        _patch_assessor_call(assessor, _stub_response(arguments=""))

        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            await assessor._call_api("prompt")

        assert exc_info.value.code == DeepSeekResponseDefect.ARGUMENTS_NOT_JSON


# truncation 観測ログの不変条件


def _stub_truncated_response(*, completion_tokens: int = 512) -> MagicMock:
    """finish_reason="length" で JSON が切れた状況の stub。"""
    return _stub_response(
        arguments="not json at all",
        finish_reason="length",
        completion_tokens=completion_tokens,
    )


class TestDeepSeekTruncationObservabilityLog:
    """defect 経路で truncation 観測ログが出る不変条件。

    - ログが出ること / 各フィールドが stub した具体値であること
    - 例外契約(re-raise)が変わっていないこと
    - 正常系ではログが出ないこと
    を 1 テスト = 1 アサーション原則に従い分割して pin する。
    """

    @pytest.mark.asyncio
    async def test_defect_emits_warning_event(self) -> None:
        """defect 経路で response_defect イベントが 1 件 emit される。"""
        assessor = DeepSeekAssessor()
        _patch_assessor_call(assessor, _stub_truncated_response())

        with capture_logs() as logs:
            with pytest.raises(AssessmentResponseInvalidError):
                await assessor._call_api("prompt")

        defect_logs = [
            e for e in logs if e.get("event") == "assessment_deepseek_response_defect"
        ]
        assert len(defect_logs) == 1

    @pytest.mark.asyncio
    async def test_defect_log_carries_finish_reason(self) -> None:
        """観測ログの finish_reason が stub した値 ``"length"`` であること。"""
        assessor = DeepSeekAssessor()
        _patch_assessor_call(assessor, _stub_truncated_response())

        with capture_logs() as logs:
            with pytest.raises(AssessmentResponseInvalidError):
                await assessor._call_api("prompt")

        log = next(
            e for e in logs if e.get("event") == "assessment_deepseek_response_defect"
        )
        assert log["finish_reason"] == "length"

    @pytest.mark.asyncio
    async def test_defect_log_carries_completion_tokens(self) -> None:
        """観測ログの completion_tokens が stub した具体値 512 であること。

        MagicMock のままだと等価比較が通ってしまうため、stub で必ず整数値を設定する。
        production が捕捉をやめたら assert が落ちる非空虚な検証。
        """
        assessor = DeepSeekAssessor()
        _patch_assessor_call(assessor, _stub_truncated_response(completion_tokens=512))

        with capture_logs() as logs:
            with pytest.raises(AssessmentResponseInvalidError):
                await assessor._call_api("prompt")

        log = next(
            e for e in logs if e.get("event") == "assessment_deepseek_response_defect"
        )
        assert log["completion_tokens"] == 512

    @pytest.mark.asyncio
    async def test_defect_log_carries_max_tokens_from_spec(self) -> None:
        """観測ログの max_tokens が spec 由来の値であること。"""
        assessor = DeepSeekAssessor()
        _patch_assessor_call(assessor, _stub_truncated_response())

        with capture_logs() as logs:
            with pytest.raises(AssessmentResponseInvalidError):
                await assessor._call_api("prompt")

        log = next(
            e for e in logs if e.get("event") == "assessment_deepseek_response_defect"
        )
        assert log["max_tokens"] == DEEPSEEK_ASSESSMENT_SPEC.gen_config["max_tokens"]

    @pytest.mark.asyncio
    async def test_defect_log_carries_code(self) -> None:
        """観測ログの code が ``ARGUMENTS_NOT_JSON`` であること。"""
        assessor = DeepSeekAssessor()
        _patch_assessor_call(assessor, _stub_truncated_response())

        with capture_logs() as logs:
            with pytest.raises(AssessmentResponseInvalidError):
                await assessor._call_api("prompt")

        log = next(
            e for e in logs if e.get("event") == "assessment_deepseek_response_defect"
        )
        assert log["code"] == DeepSeekResponseDefect.ARGUMENTS_NOT_JSON

    @pytest.mark.asyncio
    async def test_defect_reraises_assessment_response_invalid_error(self) -> None:
        """ログ追加後も ARGUMENTS_NOT_JSON で re-raise されること。"""
        assessor = DeepSeekAssessor()
        _patch_assessor_call(assessor, _stub_truncated_response())

        with capture_logs():
            with pytest.raises(AssessmentResponseInvalidError) as exc_info:
                await assessor._call_api("prompt")

        assert exc_info.value.code == DeepSeekResponseDefect.ARGUMENTS_NOT_JSON

    @pytest.mark.asyncio
    async def test_success_does_not_emit_defect_log(self) -> None:
        """正常系では ``assessment_deepseek_response_defect`` が emit されないこと。"""
        assessor = DeepSeekAssessor()
        args = json.dumps(
            {"category": "ai", "investor_take": "Positive signal.", "key_points": []}
        )
        _patch_assessor_call(
            assessor,
            _stub_response(
                arguments=args,
                finish_reason="tool_calls",
                completion_tokens=100,
            ),
        )

        with capture_logs() as logs:
            await assessor._call_api("prompt")

        defect_logs = [
            e for e in logs if e.get("event") == "assessment_deepseek_response_defect"
        ]
        assert defect_logs == []
