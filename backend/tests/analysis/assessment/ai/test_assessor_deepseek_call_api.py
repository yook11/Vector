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
- finish_reason="length" (出力切り詰め) → ``AIProviderOutputTruncatedError``
  (tool_call 検査より前に判定し、``ARGUMENTS_NOT_JSON`` には落ちない)
"""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai_providers.deepseek.error_translator import DeepSeekStateReason
from app.ai_providers.errors import AIProviderOutputTruncatedError
from app.analysis.assessment.ai.deepseek import (
    DeepSeekAssessor,
    DeepSeekResponseDefect,
)
from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.ai.parse import AssessmentResponseDefect
from app.analysis.assessment.ai.spec import DEEPSEEK_ASSESSMENT_SPEC
from app.analysis.assessment.domain.result import InScope, InScopeCategory, OutOfScope
from app.analysis.assessment.errors import AssessmentResponseInvalidError


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
    assessor._client.chat.completions.create = mock_call
    return mock_call


class TestDeepSeekCallApiSuccess:
    @pytest.mark.asyncio
    async def test_in_scope_round_trip(self, make_assessment_logger) -> None:
        """対象内のSDK応答から判定結果と監査用の応答・モデル情報を復元する。"""
        assessor = DeepSeekAssessor(MagicMock())
        args = json.dumps(
            {
                "category": "ai",
                "investor_take": "Significant traction.",
                "key_points": [],
            }
        )
        _patch_assessor_call(assessor, _stub_response(arguments=args))

        call = await assessor._call_api("prompt", logger=make_assessment_logger())

        assert isinstance(call, AssessmentCall)
        assert isinstance(call.result, InScope)
        assert call.result.category == InScopeCategory.AI
        assert call.raw_response == args
        assert call.raw_category == "ai"
        assert call.prompt_version == DEEPSEEK_ASSESSMENT_SPEC.version
        assert call.model_name == DEEPSEEK_ASSESSMENT_SPEC.model

    @pytest.mark.asyncio
    async def test_out_of_scope_round_trip(self, make_assessment_logger) -> None:
        """対象外のSDK応答を正常な対象外結果として返す。"""
        assessor = DeepSeekAssessor(MagicMock())
        args = json.dumps(
            {
                "category": "out_of_scope",
                "investor_take": "Not relevant.",
                "key_points": [],
            }
        )
        _patch_assessor_call(assessor, _stub_response(arguments=args))

        call = await assessor._call_api("prompt", logger=make_assessment_logger())

        assert isinstance(call.result, OutOfScope)
        assert call.raw_category == "out_of_scope"
        assert call.model_name == DEEPSEEK_ASSESSMENT_SPEC.model

    @pytest.mark.asyncio
    async def test_structured_output_mechanism_reaches_sdk(
        self, make_assessment_logger
    ) -> None:
        """機構 (forced tool_choice + thinking 無効) を structured_output に分離後も
        tuning (max_tokens) と共に create kwargs に届くこと。"""
        assessor = DeepSeekAssessor(MagicMock())
        args = json.dumps({"category": "ai", "investor_take": "x", "key_points": []})
        mock_call = _patch_assessor_call(assessor, _stub_response(arguments=args))

        await assessor._call_api("prompt", logger=make_assessment_logger())

        kwargs = mock_call.await_args.kwargs
        assert (
            kwargs["tool_choice"]["function"]["name"]
            == DEEPSEEK_ASSESSMENT_SPEC.tool_name
        )
        assert kwargs["extra_body"]["thinking"]["type"] == "disabled"
        assert kwargs["max_tokens"] == 1536


# tool_call 欠落 / wrong name → AssessmentResponseInvalidError (recoverable)


class TestDeepSeekToolCallStructure:
    """tool_call 構造違反は AssessmentResponseInvalidError で raise する。

    AIProviderRequestInvalidError (terminal-skip) で raise しないのは、
    provider は応答したが構造が違っただけ → モデル一時的な揺らぎを「リトライ
    無駄」扱いにしないため (recoverable で cron 救済対象)。
    """

    @pytest.mark.asyncio
    async def test_no_tool_call_raises_deepseek_no_tool_call(
        self, make_assessment_logger
    ) -> None:
        """tool_call 欠落は adapter 所有 ``NO_TOOL_CALL`` defect を焼く。

        spec は tool_choice で呼び出しを強制しているため、欠落は provider が機構
        契約を破った状態 = code (語彙) で可視化する (retryability は recoverable 維持)。
        """
        assessor = DeepSeekAssessor(MagicMock())
        _patch_assessor_call(
            assessor,
            _stub_response(arguments=None, no_tool_calls=True, finish_reason="stop"),
        )

        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            await assessor._call_api("prompt", logger=make_assessment_logger())

        assert exc_info.value.code == DeepSeekResponseDefect.NO_TOOL_CALL

    @pytest.mark.asyncio
    async def test_wrong_tool_name_raises_deepseek_wrong_tool_name(
        self, make_assessment_logger
    ) -> None:
        """要求したものと異なるツール名を専用の応答不正コードで拒否する。"""
        assessor = DeepSeekAssessor(MagicMock())
        _patch_assessor_call(
            assessor,
            _stub_response(arguments="{}", tool_name="some_other_tool"),
        )

        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            await assessor._call_api("prompt", logger=make_assessment_logger())

        assert exc_info.value.code == DeepSeekResponseDefect.WRONG_TOOL_NAME


# arguments 不正 payload 経路


class TestDeepSeekInvalidArguments:
    @pytest.mark.asyncio
    async def test_invalid_arguments_json_raises_deepseek_arguments_not_json(
        self,
        make_assessment_logger,
    ) -> None:
        """arguments が非 JSON → adapter 所有 ``ARGUMENTS_NOT_JSON`` defect。"""
        assessor = DeepSeekAssessor(MagicMock())
        _patch_assessor_call(assessor, _stub_response(arguments="not json at all"))

        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            await assessor._call_api("prompt", logger=make_assessment_logger())

        assert exc_info.value.code == DeepSeekResponseDefect.ARGUMENTS_NOT_JSON

    @pytest.mark.asyncio
    async def test_non_object_arguments_raises_deepseek_arguments_not_dict(
        self,
        make_assessment_logger,
    ) -> None:
        """JSONでもオブジェクトでないツール引数は応答不正として拒否する。"""
        assessor = DeepSeekAssessor(MagicMock())
        _patch_assessor_call(assessor, _stub_response(arguments="[1, 2, 3]"))

        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            await assessor._call_api("prompt", logger=make_assessment_logger())

        assert exc_info.value.code == DeepSeekResponseDefect.ARGUMENTS_NOT_DICT

    @pytest.mark.asyncio
    async def test_missing_key_arguments_surfaces_parse_defect(
        self, make_assessment_logger
    ) -> None:
        """parse の内容違反 (key 欠落) が adapter を素通りして焼かれる。"""
        assessor = DeepSeekAssessor(MagicMock())
        args = json.dumps({"category": "ai"})  # investor_take 欠落
        _patch_assessor_call(assessor, _stub_response(arguments=args))

        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            await assessor._call_api("prompt", logger=make_assessment_logger())

        assert exc_info.value.code == AssessmentResponseDefect.INVESTOR_TAKE_KEY_MISSING

    @pytest.mark.asyncio
    async def test_empty_arguments_raises_deepseek_arguments_not_json(
        self, make_assessment_logger
    ) -> None:
        """arguments が空文字 → JSON parse 失敗 → ``ARGUMENTS_NOT_JSON``。"""
        assessor = DeepSeekAssessor(MagicMock())
        _patch_assessor_call(assessor, _stub_response(arguments=""))

        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            await assessor._call_api("prompt", logger=make_assessment_logger())

        assert exc_info.value.code == DeepSeekResponseDefect.ARGUMENTS_NOT_JSON


# finish_reason="length" (出力切り詰め) の分類 + 観測ログの不変条件


def _stub_truncated_response(*, completion_tokens: int = 1500) -> MagicMock:
    """finish_reason="length" で JSON が切れた状況の stub。"""
    return _stub_response(
        arguments="not json at all",
        finish_reason="length",
        completion_tokens=completion_tokens,
    )


class TestDeepSeekTruncatedFinishReason:
    """finish_reason="length" は tool_call / arguments 検査より前に判定される。

    切り詰めは JSON 破損 (``ARGUMENTS_NOT_JSON``) と別分類にする不変条件を pin する。
    """

    @pytest.mark.asyncio
    async def test_truncated_raises_output_truncated_error(
        self, make_assessment_logger
    ) -> None:
        """arguments が壊れた JSON でも
        ``AssessmentResponseInvalidError`` にはならない。"""
        assessor = DeepSeekAssessor(MagicMock())
        _patch_assessor_call(assessor, _stub_truncated_response())

        with pytest.raises(AIProviderOutputTruncatedError) as exc_info:
            await assessor._call_api("prompt", logger=make_assessment_logger())

        assert exc_info.value.reason == DeepSeekStateReason.OUTPUT_TOKEN_LIMIT_REACHED

    @pytest.mark.asyncio
    async def test_truncated_with_valid_json_arguments_still_raises(
        self, make_assessment_logger
    ) -> None:
        """arguments が偶然 valid JSON でも finish_reason 判定が parse より先に効く。"""
        assessor = DeepSeekAssessor(MagicMock())
        args = json.dumps({"category": "ai", "investor_take": "x", "key_points": []})
        _patch_assessor_call(
            assessor,
            _stub_response(arguments=args, finish_reason="length"),
        )

        with pytest.raises(AIProviderOutputTruncatedError):
            await assessor._call_api("prompt", logger=make_assessment_logger())


class TestDeepSeekObservabilityLog:
    """記事分析ポリシーを通したJSONで、AI呼び出し固有の診断を確認する。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("completion_tokens", [0, 1500])
    async def test_truncated_log_keeps_usage_and_fixed_reason(
        self, make_assessment_logger, capsys, completion_tokens
    ):
        """打ち切りの使用量と固定理由が、相関情報・モデルとともに出力される。"""
        assessor = DeepSeekAssessor(MagicMock())
        _patch_assessor_call(
            assessor, _stub_truncated_response(completion_tokens=completion_tokens)
        )
        with pytest.raises(AIProviderOutputTruncatedError):
            await assessor.assess(
                "PRIVATE_TITLE", "PRIVATE_SUMMARY", logger=make_assessment_logger()
            )

        output = capsys.readouterr().out
        record = next(
            json.loads(line)
            for line in output.splitlines()
            if json.loads(line)["event"] == "assessment_deepseek_output_truncated"
        )
        assert record["output_tokens"] == completion_tokens
        assert record["max_output_tokens"] == 1536
        assert record["reason"] == "output_token_limit_reached"
        assert record["message_id"] == "message-001"
        assert record["model"] == DEEPSEEK_ASSESSMENT_SPEC.model
        assert record["level"] == "warning"
        assert "PRIVATE_" not in output

    @pytest.mark.asyncio
    @pytest.mark.parametrize("completion_tokens", [None, -1, True, "PRIVATE_USAGE"])
    async def test_unavailable_or_invalid_usage_is_omitted(
        self, make_assessment_logger, capsys, completion_tokens
    ):
        """未取得または非負整数でない使用量をログへ渡さない。"""
        assessor = DeepSeekAssessor(MagicMock())
        _patch_assessor_call(
            assessor, _stub_truncated_response(completion_tokens=completion_tokens)
        )
        with pytest.raises(AIProviderOutputTruncatedError):
            await assessor._call_api("prompt", logger=make_assessment_logger())
        record = json.loads(capsys.readouterr().out)
        assert "output_tokens" not in record

    @pytest.mark.asyncio
    @pytest.mark.parametrize("max_tokens", [None, -1, True, "PRIVATE_LIMIT"])
    async def test_unavailable_or_invalid_output_limit_is_omitted(
        self, make_assessment_logger, capsys, max_tokens
    ):
        """設定に有効な非負整数の出力上限がない場合、その項目を出さない。"""
        assessor = DeepSeekAssessor(MagicMock())
        assessor.SPEC = replace(
            DEEPSEEK_ASSESSMENT_SPEC, gen_config={"max_tokens": max_tokens}
        )
        _patch_assessor_call(assessor, _stub_truncated_response())
        with pytest.raises(AIProviderOutputTruncatedError):
            await assessor._call_api("prompt", logger=make_assessment_logger())
        record = json.loads(capsys.readouterr().out)
        assert "max_output_tokens" not in record

    @pytest.mark.asyncio
    async def test_zero_output_limit_is_preserved(self, make_assessment_logger, capsys):
        """出力上限の0を欠損扱いで省略しない。"""
        assessor = DeepSeekAssessor(MagicMock())
        assessor.SPEC = replace(DEEPSEEK_ASSESSMENT_SPEC, gen_config={"max_tokens": 0})
        _patch_assessor_call(assessor, _stub_truncated_response())
        with pytest.raises(AIProviderOutputTruncatedError):
            await assessor._call_api("prompt", logger=make_assessment_logger())
        assert json.loads(capsys.readouterr().out)["max_output_tokens"] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "finish_reason", ["stop", "tool_calls", "content_filter", "function_call"]
    )
    async def test_response_defect_keeps_code_and_known_finish_reason(
        self, make_assessment_logger, capsys, finish_reason
    ):
        """応答本文を出さず、契約違反コードと既知の終了理由・使用量を記録する。"""
        assessor = DeepSeekAssessor(MagicMock())
        _patch_assessor_call(
            assessor,
            _stub_response(
                arguments="PRIVATE_RESPONSE",
                finish_reason=finish_reason,
                completion_tokens=42,
            ),
        )
        with pytest.raises(AssessmentResponseInvalidError):
            await assessor.assess(
                "PRIVATE_TITLE", "PRIVATE_SUMMARY", logger=make_assessment_logger()
            )
        output = capsys.readouterr().out
        record = next(
            json.loads(line)
            for line in output.splitlines()
            if json.loads(line)["event"] == "assessment_deepseek_response_defect"
        )
        assert record["code"] == "assessment_response_deepseek_arguments_not_json"
        assert record["finish_reason"] == finish_reason
        assert record["output_tokens"] == 42
        assert record["max_output_tokens"] == 1536
        assert "PRIVATE_" not in output
        assert "causes" not in record

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "finish_reason", ["PRIVATE_UNKNOWN_REASON", None, ["PRIVATE_REASON"]]
    )
    async def test_unknown_finish_reason_is_omitted(
        self, make_assessment_logger, capsys, finish_reason
    ):
        """未知の終了理由を文字列化せず、契約違反コードだけを残す。"""
        assessor = DeepSeekAssessor(MagicMock())
        _patch_assessor_call(
            assessor,
            _stub_response(arguments="PRIVATE_RESPONSE", finish_reason=finish_reason),
        )
        with pytest.raises(AssessmentResponseInvalidError):
            await assessor._call_api("prompt", logger=make_assessment_logger())
        output = capsys.readouterr().out
        record = json.loads(output)
        assert "finish_reason" not in record
        assert record["code"] == "assessment_response_deepseek_arguments_not_json"
        assert "PRIVATE_" not in output

    @pytest.mark.asyncio
    async def test_truncation_does_not_log_response_defect(
        self, make_assessment_logger, capsys
    ):
        """打ち切りを応答JSONの契約違反として重ねて記録しない。"""
        assessor = DeepSeekAssessor(MagicMock())
        _patch_assessor_call(assessor, _stub_truncated_response())
        with pytest.raises(AIProviderOutputTruncatedError):
            await assessor._call_api("prompt", logger=make_assessment_logger())
        events = [
            json.loads(line)["event"] for line in capsys.readouterr().out.splitlines()
        ]
        assert "assessment_deepseek_response_defect" not in events

    @pytest.mark.asyncio
    async def test_success_does_not_log_response_warning(
        self, make_assessment_logger, capsys
    ):
        """正常応答では打ち切りや契約違反の警告を出さない。"""
        assessor = DeepSeekAssessor(MagicMock())
        _patch_assessor_call(
            assessor,
            _stub_response(
                arguments=json.dumps(
                    {"category": "ai", "investor_take": "x", "key_points": []}
                )
            ),
        )
        await assessor._call_api("prompt", logger=make_assessment_logger())
        assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_assessor_borrows_client_without_reading_secret_or_closing(
    monkeypatch, make_assessment_logger
):
    """Assessorは秘密情報が未設定でも注入されたクライアントで判定し、そのクライアントを閉じない。"""
    from pydantic import SecretStr

    from app.config import settings

    monkeypatch.setattr(settings, "deepseek_api_key", SecretStr(""))
    client = MagicMock()
    client.close = AsyncMock()
    assessor = DeepSeekAssessor(client)
    _patch_assessor_call(
        assessor,
        _stub_response(
            arguments=json.dumps(
                {
                    "category": "out_of_scope",
                    "investor_take": "Outside",
                    "key_points": [],
                }
            )
        ),
    )
    await assessor.assess("title", "summary", logger=make_assessment_logger())
    assert assessor._client is client
    client.chat.completions.create.assert_awaited_once()
    client.close.assert_not_awaited()
