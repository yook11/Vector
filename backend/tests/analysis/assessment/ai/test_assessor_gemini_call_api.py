"""``GeminiAssessor._call_api`` の応答分類テスト。

検証:
- 正常系: in-scope / out-of-scope の round-trip と envelope field 値
- 応答本文が JSON でない / object でない → ``AssessmentResponseInvalidError``
  (adapter 所有の defect)。内容の schema 違反は parse の defect がそのまま届く
- 入力ブロック・出力拒否・出力上限での打ち切り → ``AIProviderResultError``
  (打ち切りは本文の検査より前に判定し、``NOT_JSON`` には落ちない)
- 記事分析ポリシーを通した JSON ログに、本文を出さず診断だけが残る
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.genai.types import (
    BlockedReason,
    Candidate,
    Content,
    FinishReason,
    GenerateContentResponse,
    GenerateContentResponsePromptFeedback,
    GenerateContentResponseUsageMetadata,
    Part,
)

from app.ai_providers.errors import (
    AIProviderResultError,
    AIProviderResultReason,
)
from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.ai.gemini import GeminiAssessor, GeminiResponseDefect
from app.analysis.assessment.ai.parse import AssessmentResponseDefect
from app.analysis.assessment.ai.spec import GEMINI_ASSESSMENT_SPEC
from app.analysis.assessment.domain.result import InScope, InScopeCategory, OutOfScope
from app.analysis.assessment.errors import AssessmentResponseInvalidError


def _response(
    *,
    text: str | None,
    finish_reason: FinishReason | None = FinishReason.STOP,
    candidates_token_count: int | None = None,
    thoughts_token_count: int | None = None,
    prompt_block: bool = False,
) -> GenerateContentResponse:
    candidate = Candidate(
        finish_reason=finish_reason,
        content=Content(role="model", parts=[Part(text=text)]) if text else None,
    )
    usage = (
        GenerateContentResponseUsageMetadata(
            candidates_token_count=candidates_token_count,
            thoughts_token_count=thoughts_token_count,
        )
        if candidates_token_count is not None
        else None
    )
    return GenerateContentResponse(
        candidates=[] if prompt_block else [candidate],
        usage_metadata=usage,
        prompt_feedback=GenerateContentResponsePromptFeedback(
            block_reason=BlockedReason.SAFETY
        )
        if prompt_block
        else None,
    )


def _assessor(response: GenerateContentResponse) -> GeminiAssessor:
    client = MagicMock()
    client.models.generate_content = AsyncMock(return_value=response)
    return GeminiAssessor(client=client)


def _payload(category: str = "ai") -> str:
    return json.dumps(
        {"category": category, "investor_take": "Significant.", "key_points": []}
    )


class TestGeminiCallApiSuccess:
    @pytest.mark.asyncio
    async def test_in_scope_round_trip(self, make_assessment_logger) -> None:
        """対象内の応答から判定結果と監査用の応答・モデル情報を復元する。"""
        text = _payload("ai")
        assessor = _assessor(_response(text=text))

        call = await assessor._call_api("prompt", logger=make_assessment_logger())

        assert isinstance(call, AssessmentCall)
        assert isinstance(call.result, InScope)
        assert call.result.category == InScopeCategory.AI
        assert call.raw_response == text
        assert call.raw_category == "ai"
        assert call.prompt_version == GEMINI_ASSESSMENT_SPEC.version
        assert call.model_name == "gemini-3.5-flash-lite"

    @pytest.mark.asyncio
    async def test_out_of_scope_round_trip(self, make_assessment_logger) -> None:
        """対象外の応答を正常な対象外結果として返す。"""
        assessor = _assessor(_response(text=_payload("out_of_scope")))

        call = await assessor._call_api("prompt", logger=make_assessment_logger())

        assert isinstance(call.result, OutOfScope)
        assert call.raw_category == "out_of_scope"

    @pytest.mark.asyncio
    async def test_request_carries_declared_model_and_structured_output(
        self, make_assessment_logger
    ) -> None:
        """宣言したモデル・出力上限・JSON 指定・schema が SDK 呼び出しに届く。"""
        assessor = _assessor(_response(text=_payload()))

        await assessor._call_api("PROMPT_SENTINEL", logger=make_assessment_logger())

        kwargs = assessor._client.models.generate_content.await_args.kwargs
        config = kwargs["config"]
        assert (kwargs["model"], kwargs["contents"]) == (
            "gemini-3.5-flash-lite",
            "PROMPT_SENTINEL",
        )
        assert config.max_output_tokens == 4096
        assert config.response_mime_type == "application/json"
        assert config.response_schema == dict(GEMINI_ASSESSMENT_SPEC.response_schema)
        assert config.temperature is None


class TestGeminiResponseBody:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", ["not json", None])
    async def test_unreadable_body_raises_not_json(
        self, make_assessment_logger, text
    ) -> None:
        """JSON として読めない本文 (空を含む) は adapter 所有の NOT_JSON。"""
        assessor = _assessor(_response(text=text))

        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            await assessor._call_api("prompt", logger=make_assessment_logger())

        assert exc_info.value.defect is GeminiResponseDefect.NOT_JSON

    @pytest.mark.asyncio
    async def test_non_object_body_raises_not_object(
        self, make_assessment_logger
    ) -> None:
        """JSON でも object でない本文は NOT_OBJECT。"""
        assessor = _assessor(_response(text='["ai"]'))

        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            await assessor._call_api("prompt", logger=make_assessment_logger())

        assert exc_info.value.defect is GeminiResponseDefect.NOT_OBJECT

    @pytest.mark.asyncio
    async def test_missing_key_surfaces_parse_defect(
        self, make_assessment_logger
    ) -> None:
        """内容の schema 違反は parse が所有する defect のまま届く。"""
        text = json.dumps({"investor_take": "x", "key_points": []})
        assessor = _assessor(_response(text=text))

        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            await assessor._call_api("prompt", logger=make_assessment_logger())

        assert exc_info.value.defect is AssessmentResponseDefect.CATEGORY_KEY_MISSING


# finish_reason → 出力拒否の reason の期待写像 (production の dict とは独立な literal)。
_OUTPUT_BLOCKED: dict[FinishReason, AIProviderResultReason] = {
    FinishReason.SAFETY: AIProviderResultReason.OUTPUT_BLOCKED_SAFETY,
    FinishReason.RECITATION: AIProviderResultReason.OUTPUT_BLOCKED_RECITATION,
    FinishReason.BLOCKLIST: AIProviderResultReason.OUTPUT_BLOCKED_BLOCKLIST,
    FinishReason.PROHIBITED_CONTENT: (
        AIProviderResultReason.OUTPUT_BLOCKED_PROHIBITED_CONTENT
    ),
    FinishReason.SPII: AIProviderResultReason.OUTPUT_BLOCKED_SPII,
}


class TestGeminiProviderResult:
    @pytest.mark.asyncio
    async def test_prompt_block_raises_input_blocked(
        self, make_assessment_logger
    ) -> None:
        """入力がブロックされた応答は、本文を読まずに入力ブロックとする。"""
        assessor = _assessor(_response(text=None, prompt_block=True))

        with pytest.raises(AIProviderResultError) as exc_info:
            await assessor._call_api("prompt", logger=make_assessment_logger())

        assert exc_info.value.reason is AIProviderResultReason.INPUT_BLOCKED

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("finish_reason", "reason"), list(_OUTPUT_BLOCKED.items()))
    async def test_output_block_raises_matching_reason(
        self, make_assessment_logger, finish_reason, reason
    ) -> None:
        """出力拒否の終了理由を、それぞれの拒否理由に分類する。"""
        assessor = _assessor(_response(text=_payload(), finish_reason=finish_reason))

        with pytest.raises(AIProviderResultError) as exc_info:
            await assessor._call_api("prompt", logger=make_assessment_logger())

        assert exc_info.value.reason is reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", ['{"category": "ai", "inv', _payload()])
    async def test_max_tokens_raises_output_truncated_before_body_check(
        self, make_assessment_logger, text
    ) -> None:
        """打ち切りは本文が壊れていても読めても、本文の検査より先に判定する。"""
        assessor = _assessor(
            _response(text=text, finish_reason=FinishReason.MAX_TOKENS)
        )

        with pytest.raises(AIProviderResultError) as exc_info:
            await assessor._call_api("prompt", logger=make_assessment_logger())

        assert exc_info.value.reason is AIProviderResultReason.OUTPUT_TRUNCATED


def _records(output: str, event: str) -> list[dict[str, object]]:
    return [
        record
        for record in (json.loads(line) for line in output.splitlines())
        if record["event"] == event
    ]


class TestGeminiObservabilityLog:
    """記事分析ポリシーを通した JSON で、AI 呼び出し固有の診断を確認する。"""

    @pytest.mark.asyncio
    async def test_truncated_log_counts_thinking_against_the_output_limit(
        self, make_assessment_logger, capsys
    ) -> None:
        """打ち切りの出力トークン数は thinking を含め、出力上限と並べて記録する。"""
        assessor = _assessor(
            _response(
                text="PRIVATE_RESPONSE",
                finish_reason=FinishReason.MAX_TOKENS,
                candidates_token_count=4000,
                thoughts_token_count=96,
            )
        )
        with pytest.raises(AIProviderResultError):
            await assessor.assess(
                "PRIVATE_TITLE", "PRIVATE_SUMMARY", logger=make_assessment_logger()
            )

        output = capsys.readouterr().out
        [record] = _records(output, "assessment_gemini_output_truncated")
        assert record["output_tokens"] == 4096
        assert record["max_output_tokens"] == 4096
        assert record["reason"] == "output_truncated"
        assert record["message_id"] == "message-001"
        assert record["model"] == "gemini-3.5-flash-lite"
        assert record["level"] == "warning"
        assert "PRIVATE_" not in output
        assert _records(output, "assessment_gemini_response_defect") == []

    @pytest.mark.asyncio
    async def test_unavailable_usage_is_omitted(
        self, make_assessment_logger, capsys
    ) -> None:
        """使用量が返らない応答では出力トークン数を出さず、上限だけを残す。"""
        assessor = _assessor(
            _response(text=None, finish_reason=FinishReason.MAX_TOKENS)
        )
        with pytest.raises(AIProviderResultError):
            await assessor._call_api("prompt", logger=make_assessment_logger())

        record = json.loads(capsys.readouterr().out)
        assert "output_tokens" not in record
        assert record["max_output_tokens"] == 4096

    @pytest.mark.asyncio
    async def test_response_defect_keeps_code_and_finish_reason(
        self, make_assessment_logger, capsys
    ) -> None:
        """応答本文を出さず、契約違反コードと終了理由・使用量を記録する。"""
        assessor = _assessor(
            _response(text="PRIVATE_RESPONSE", candidates_token_count=42)
        )
        with pytest.raises(AssessmentResponseInvalidError):
            await assessor.assess(
                "PRIVATE_TITLE", "PRIVATE_SUMMARY", logger=make_assessment_logger()
            )

        output = capsys.readouterr().out
        [record] = _records(output, "assessment_gemini_response_defect")
        assert record["code"] == "assessment_response_gemini_not_json"
        assert record["finish_reason"] == "STOP"
        assert record["output_tokens"] == 42
        assert record["max_output_tokens"] == 4096
        assert "PRIVATE_" not in output
        assert "related_exceptions" not in record

    @pytest.mark.asyncio
    async def test_missing_finish_reason_is_omitted(
        self, make_assessment_logger, capsys
    ) -> None:
        """終了理由が返らない応答では、その項目を出さずに契約違反コードを残す。"""
        assessor = _assessor(_response(text="PRIVATE_RESPONSE", finish_reason=None))
        with pytest.raises(AssessmentResponseInvalidError):
            await assessor._call_api("prompt", logger=make_assessment_logger())

        record = json.loads(capsys.readouterr().out)
        assert "finish_reason" not in record
        assert record["code"] == "assessment_response_gemini_not_json"

    @pytest.mark.asyncio
    async def test_success_does_not_log_response_warning(
        self, make_assessment_logger, capsys
    ) -> None:
        """正常応答では打ち切りや契約違反の警告を出さない。"""
        assessor = _assessor(_response(text=_payload()))

        await assessor._call_api("prompt", logger=make_assessment_logger())

        assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_assessor_borrows_client_without_closing(make_assessment_logger):
    """Assessorは注入されたクライアントで判定し、そのクライアントを閉じない。"""
    client = MagicMock()
    client.models.generate_content = AsyncMock(
        return_value=_response(text=_payload("out_of_scope"))
    )
    client.aclose = AsyncMock()
    assessor = GeminiAssessor(client=client)

    await assessor.assess("title", "summary", logger=make_assessment_logger())

    assert assessor._client is client
    client.models.generate_content.assert_awaited_once()
    client.aclose.assert_not_awaited()
