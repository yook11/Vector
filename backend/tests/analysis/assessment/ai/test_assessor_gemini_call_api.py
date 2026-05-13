"""``GeminiAssessor._call_api`` の integration テスト。

PR3 で次の流れに rewrite された:
- SDK レスポンス text を ``json.loads`` で dict 化
- ``parse_assessment`` でドメイン型 (``InScope`` / ``OutOfScope``) に詰め替え
- raw 情報と共に ``AssessmentCall`` envelope に格納

検証:
- 正常系: in-scope / out-of-scope の round-trip と envelope field 値
- finish_reason == SAFETY / RECITATION で ``AIProviderOutputBlockedError`` raise
- text が JSON 不正 → ``AssessmentResponseInvalidError`` raise
- text が JSON object でない (list 等) → ``AssessmentResponseInvalidError`` raise
- response_schema が dict (``ASSESSMENT_GEMINI_SCHEMA``) で渡される
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from app.analysis.ai_provider_errors import AIProviderOutputBlockedError
from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.ai.gemini import GeminiAssessor
from app.analysis.assessment.ai.spec import GEMINI_ASSESSMENT_SPEC
from app.analysis.assessment.domain.result import InScope, InScopeCategory, OutOfScope
from app.analysis.assessment.errors import AssessmentResponseInvalidError
from app.config import settings


@pytest.fixture(autouse=True)
def _set_gemini_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "gemini_api_key", SecretStr("test-key"))


def _stub_response(text: str, *, finish_reason_name: str | None = None) -> MagicMock:
    """SDK Response の最小 stub (text 属性と candidates[0].finish_reason を持つ)。"""
    response = MagicMock()
    response.text = text
    if finish_reason_name is None:
        response.candidates = []
    else:
        candidate = MagicMock()
        candidate.finish_reason = MagicMock(name=finish_reason_name)
        # MagicMock(name=...) は repr に使う名前で、属性 .name には反映されない。
        # 明示的に .name 属性を設定する。
        candidate.finish_reason.name = finish_reason_name
        response.candidates = [candidate]
    return response


def _patch_assessor_call(assessor: GeminiAssessor, response: MagicMock) -> AsyncMock:
    """assessor._client.aio.models.generate_content を mock に差し替える。"""
    mock_call = AsyncMock(return_value=response)
    assessor._client = MagicMock()
    assessor._client.aio.models.generate_content = mock_call
    return mock_call


# ---------------------------------------------------------------------------
# Round trip: in-scope / out-of-scope
# ---------------------------------------------------------------------------


class TestGeminiCallApiSuccess:
    @pytest.mark.asyncio
    async def test_in_scope_round_trip(self) -> None:
        assessor = GeminiAssessor()
        text = json.dumps(
            {
                "category": "ai",
                "topic": "ai agents",
                "investor_take": "Significant traction.",
                "events": [],
            }
        )
        _patch_assessor_call(assessor, _stub_response(text))

        call = await assessor._call_api("prompt")

        assert isinstance(call, AssessmentCall)
        assert isinstance(call.result, InScope)
        assert call.result.category == InScopeCategory.AI
        assert call.result.topic.root == "ai agents"
        assert call.result.investor_take == "Significant traction."
        assert call.raw_response == text
        assert call.raw_category == "ai"
        assert call.raw_topic == "ai agents"
        assert call.prompt_version == GEMINI_ASSESSMENT_SPEC.version
        assert call.model_name == GEMINI_ASSESSMENT_SPEC.model

    @pytest.mark.asyncio
    async def test_out_of_scope_round_trip(self) -> None:
        assessor = GeminiAssessor()
        text = json.dumps(
            {
                "category": "out_of_scope",
                "topic": "ignored",
                "investor_take": "Not relevant.",
                "events": [],
            }
        )
        _patch_assessor_call(assessor, _stub_response(text))

        call = await assessor._call_api("prompt")

        assert isinstance(call.result, OutOfScope)
        assert call.result.investor_take == "Not relevant."
        assert call.raw_category == "out_of_scope"
        assert call.raw_topic == "ignored"  # OutOfScope 経路でも raw_topic 保持
        assert call.model_name == GEMINI_ASSESSMENT_SPEC.model

    @pytest.mark.asyncio
    async def test_uses_dict_response_schema(self) -> None:
        assessor = GeminiAssessor()
        text = json.dumps(
            {"category": "ai", "topic": "ai", "investor_take": "x", "events": []}
        )
        mock_call = _patch_assessor_call(assessor, _stub_response(text))

        await assessor._call_api("prompt")

        # generate_content が呼ばれた config 引数の response_schema が dict であること
        kwargs = mock_call.await_args.kwargs
        config = kwargs["config"]
        # GenerateContentConfig instance なので response_schema 属性で確認
        assert isinstance(config.response_schema, dict)
        assert config.response_schema.get("type") == "OBJECT"


# ---------------------------------------------------------------------------
# finish_reason 経路 (translate_error 経由ではなく _call_api 内で直接 raise)
# ---------------------------------------------------------------------------


class TestGeminiFinishReasonBlocked:
    @pytest.mark.asyncio
    async def test_finish_reason_safety_raises_blocked(self) -> None:
        assessor = GeminiAssessor()
        text = json.dumps(
            {"category": "ai", "topic": "ai", "investor_take": "x", "events": []}
        )
        _patch_assessor_call(
            assessor, _stub_response(text, finish_reason_name="SAFETY")
        )

        with pytest.raises(AIProviderOutputBlockedError) as exc_info:
            await assessor._call_api("prompt")

        assert "SAFETY" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_finish_reason_recitation_raises_blocked(self) -> None:
        assessor = GeminiAssessor()
        _patch_assessor_call(
            assessor, _stub_response("{}", finish_reason_name="RECITATION")
        )

        with pytest.raises(AIProviderOutputBlockedError) as exc_info:
            await assessor._call_api("prompt")

        assert "RECITATION" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_finish_reason_stop_does_not_raise(self) -> None:
        """正常終了の finish_reason (STOP 等) では raise せず parse に進む。"""
        assessor = GeminiAssessor()
        text = json.dumps(
            {"category": "ai", "topic": "ai", "investor_take": "x", "events": []}
        )
        _patch_assessor_call(assessor, _stub_response(text, finish_reason_name="STOP"))

        call = await assessor._call_api("prompt")
        assert isinstance(call.result, InScope)


# ---------------------------------------------------------------------------
# 不正 payload 経路: AssessmentResponseInvalidError
# ---------------------------------------------------------------------------


class TestGeminiInvalidPayload:
    @pytest.mark.asyncio
    async def test_invalid_json_raises_response_invalid(self) -> None:
        assessor = GeminiAssessor()
        _patch_assessor_call(assessor, _stub_response("not json at all"))

        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            await assessor._call_api("prompt")

        assert "not valid JSON" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_non_object_payload_raises_response_invalid(self) -> None:
        assessor = GeminiAssessor()
        # JSON array (list) は object ではないので reject
        _patch_assessor_call(assessor, _stub_response("[1, 2, 3]"))

        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            await assessor._call_api("prompt")

        assert "not a JSON object" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_missing_key_payload_raises_response_invalid(self) -> None:
        """parse_assessment の key 欠落で AssessmentResponseInvalidError raise。"""
        assessor = GeminiAssessor()
        text = json.dumps({"category": "ai"})  # topic / investor_take 欠落
        _patch_assessor_call(assessor, _stub_response(text))

        with pytest.raises(AssessmentResponseInvalidError):
            await assessor._call_api("prompt")

    @pytest.mark.asyncio
    async def test_empty_text_raises_response_invalid(self) -> None:
        """response.text が None / 空 → JSON parse 失敗 → invalid。"""
        assessor = GeminiAssessor()
        _patch_assessor_call(assessor, _stub_response(""))

        with pytest.raises(AssessmentResponseInvalidError):
            await assessor._call_api("prompt")
