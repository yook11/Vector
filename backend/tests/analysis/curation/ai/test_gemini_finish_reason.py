"""``GeminiCurator._call_api`` の finish_reason 検査テスト (PR3.5-c)。

検証する性質:
- finish_reason が SAFETY/RECITATION/BLOCKLIST/PROHIBITED_CONTENT/SPII の
  いずれかなら ``AIProviderOutputBlockedError`` (Layer 2-A) を raise する
  (Stage 3 boundary で ``CurationTerminalDropError`` に詰め替えられる)
- ``finish_reason=STOP`` (通常終了) で ``parsed`` が CurationResult なら
  ``CurationCall`` を返す
- ``finish_reason=MAX_TOKENS`` のように policy block 系 **以外** で
  ``parsed`` が CurationResult でない場合は ``CurationResponseInvalidError``
  (Layer 2-B、``CurationRecoverableError`` 派生)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from google.genai.types import (
    Candidate,
    Content,
    FinishReason,
    GenerateContentResponse,
    Part,
)

from app.analysis.ai_provider_errors import AIProviderOutputBlockedError
from app.analysis.curation.ai.envelope import CurationCall
from app.analysis.curation.ai.gemini import GeminiCurator
from app.analysis.curation.ai.gemini_spec import GEMINI_CURATION_SPEC
from app.analysis.curation.ai.schema import GeminiCurationResponse
from app.analysis.curation.domain import Signal
from app.analysis.curation.errors import CurationResponseInvalidError
from app.analysis.gemini_error_translator import GeminiContentRejectionReason


def _make_response(
    *,
    finish_reason: FinishReason | None,
    text: str = "",
    parsed: object | None = None,
) -> GenerateContentResponse:
    candidate = Candidate(
        finish_reason=finish_reason,
        content=Content(role="model", parts=[Part(text=text)]) if text else None,
    )
    response = GenerateContentResponse(candidates=[candidate])
    # ``parsed`` は内部 _parsed_response_field で計算されるが、テストでは直接 set
    if parsed is not None:
        response.parsed = parsed  # type: ignore[assignment]
    return response


def _ok_gemini_response() -> GeminiCurationResponse:
    return GeminiCurationResponse(
        relevance="signal",
        title_ja="t",
        summary_ja="s",
    )


def _make_curator(
    response: GenerateContentResponse,
) -> GeminiCurator:
    """API key check を bypass し、SDK 呼び出しを mock した extractor を返す。"""
    curator = GeminiCurator.__new__(GeminiCurator)
    curator._client = MagicMock()  # type: ignore[attr-defined]
    curator._client.aio.models.generate_content = AsyncMock(return_value=response)
    return curator


# finish_reason → content 拒否 reason の期待写像 (production の dict とは独立な
# literal。両者が一致することで adapter の finish_reason→reason 配線を検証する)。
_FINISH_REASON_TO_CONTENT_REASON: dict[FinishReason, GeminiContentRejectionReason] = {
    FinishReason.SAFETY: GeminiContentRejectionReason.SAFETY,
    FinishReason.RECITATION: GeminiContentRejectionReason.RECITATION,
    FinishReason.BLOCKLIST: GeminiContentRejectionReason.BLOCKLIST,
    FinishReason.PROHIBITED_CONTENT: GeminiContentRejectionReason.PROHIBITED_CONTENT,
    FinishReason.SPII: GeminiContentRejectionReason.SPII,
}


@pytest.mark.parametrize("blocked_reason", list(_FINISH_REASON_TO_CONTENT_REASON))
@pytest.mark.asyncio
async def test_policy_block_finish_reason_raises_output_blocked(
    blocked_reason: FinishReason,
) -> None:
    """policy block 系 finish_reason は OutputBlocked を raise し reason を運ぶ。

    AIProvider*Error は SDK 生値を ``__str__`` に載せない (SAFE_ATTRS 契約) が、
    どの policy block かは ``reason`` (PII-free な種別ラベル) で自己記述する。
    """
    response = _make_response(finish_reason=blocked_reason, text="some draft")
    curator = _make_curator(response)
    with pytest.raises(AIProviderOutputBlockedError) as ei:
        await curator._call_api("prompt")
    assert ei.value.CODE == "ai_error_output_blocked"
    assert ei.value.reason is _FINISH_REASON_TO_CONTENT_REASON[blocked_reason]


@pytest.mark.asyncio
async def test_policy_block_with_no_text_still_raises_output_blocked() -> None:
    """raw_response 空でも OutputBlocked は raise (reason で種別を識別)。"""
    response = _make_response(finish_reason=FinishReason.SAFETY, text="")
    curator = _make_curator(response)
    with pytest.raises(AIProviderOutputBlockedError) as ei:
        await curator._call_api("prompt")
    assert ei.value.CODE == "ai_error_output_blocked"
    assert ei.value.reason is GeminiContentRejectionReason.SAFETY


@pytest.mark.asyncio
async def test_stop_with_parsed_result_returns_envelope_with_signal() -> None:
    """parsed が GeminiCurationResponse(signal) なら CurationCall[Signal]
    を返す。"""
    parsed = _ok_gemini_response()
    response = _make_response(
        finish_reason=FinishReason.STOP, text='{"x":1}', parsed=parsed
    )
    curator = _make_curator(response)
    envelope = await curator._call_api("prompt")
    assert isinstance(envelope, CurationCall)
    # PR1-a: parse_curation で Signal に詰め替えられる
    # (parsed と同一 instance ではない)
    assert isinstance(envelope.result, Signal)
    assert envelope.result.title_ja == "t"
    assert envelope.raw_response == '{"x":1}'
    assert envelope.raw_relevance == "signal"
    assert envelope.prompt_version == GEMINI_CURATION_SPEC.version
    assert envelope.model_name == GEMINI_CURATION_SPEC.model


@pytest.mark.asyncio
async def test_max_tokens_without_parsed_raises_response_invalid() -> None:
    """policy block 系以外で parsed が GeminiCurationResponse でない場合は
    Layer 2-B。"""
    response = _make_response(finish_reason=FinishReason.MAX_TOKENS, text="truncated")
    curator = _make_curator(response)
    with pytest.raises(CurationResponseInvalidError) as ei:
        await curator._call_api("prompt")
    assert ei.value.code == "extraction_response_invalid"
