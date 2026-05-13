"""``GeminiExtractor._call_api`` の finish_reason 検査テスト (PR3.5-c)。

検証する性質:
- finish_reason が SAFETY/RECITATION/BLOCKLIST/PROHIBITED_CONTENT/SPII の
  いずれかなら ``AIProviderOutputBlockedError`` (Layer 2-A、
  NonRetryableDropArticle) を raise する
- ``finish_reason=STOP`` (通常終了) で ``parsed`` が ExtractionResult なら
  ``ExtractionCall`` を返す
- ``finish_reason=MAX_TOKENS`` のように policy block 系 **以外** で
  ``parsed`` が ExtractionResult でない場合は ``ExtractionResponseInvalidError``
  (Layer 2-B、RetryableError)
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
from app.analysis.domain.value_objects.entity import EntityRawType, EntitySurface
from app.analysis.extraction.ai.envelope import ExtractionCall
from app.analysis.extraction.ai.gemini import GeminiExtractor
from app.analysis.extraction.ai.gemini_prompt import GeminiExtractionPrompt
from app.analysis.extraction.ai.schema import GeminiExtractionResponse
from app.analysis.extraction.domain import ExtractedEntity, Signal
from app.analysis.extraction.errors import ExtractionResponseInvalidError


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


def _ok_gemini_response() -> GeminiExtractionResponse:
    return GeminiExtractionResponse(
        relevance="signal",
        title_ja="t",
        summary_ja="s",
        entities=[
            ExtractedEntity(
                surface=EntitySurface("X"), raw_type=EntityRawType("Company")
            )
        ],
    )


def _make_extractor(
    response: GenerateContentResponse,
) -> GeminiExtractor:
    """API key check を bypass し、SDK 呼び出しを mock した extractor を返す。"""
    extractor = GeminiExtractor.__new__(GeminiExtractor)
    extractor._client = MagicMock()  # type: ignore[attr-defined]
    extractor._client.aio.models.generate_content = AsyncMock(return_value=response)
    return extractor


@pytest.mark.parametrize(
    "blocked_reason",
    [
        FinishReason.SAFETY,
        FinishReason.RECITATION,
        FinishReason.BLOCKLIST,
        FinishReason.PROHIBITED_CONTENT,
        FinishReason.SPII,
    ],
)
@pytest.mark.asyncio
async def test_policy_block_finish_reason_raises_output_blocked(
    blocked_reason: FinishReason,
) -> None:
    """policy block 系 finish_reason は Layer 2-A の OutputBlocked を raise する。"""
    response = _make_response(finish_reason=blocked_reason, text="some draft")
    extractor = _make_extractor(response)
    with pytest.raises(AIProviderOutputBlockedError) as ei:
        await extractor._call_api("prompt")
    assert blocked_reason.name in str(ei.value)
    assert ei.value.CODE == "ai_error_output_blocked"


@pytest.mark.asyncio
async def test_policy_block_with_no_text_still_raises_output_blocked() -> None:
    """raw_response 空でも OutputBlocked は raise (message に finish_reason 含む)。"""
    response = _make_response(finish_reason=FinishReason.SAFETY, text="")
    extractor = _make_extractor(response)
    with pytest.raises(AIProviderOutputBlockedError) as ei:
        await extractor._call_api("prompt")
    assert "SAFETY" in str(ei.value)


@pytest.mark.asyncio
async def test_stop_with_parsed_result_returns_envelope_with_signal() -> None:
    """parsed が GeminiExtractionResponse(signal) なら ExtractionCall[Signal]
    を返す。"""
    parsed = _ok_gemini_response()
    response = _make_response(
        finish_reason=FinishReason.STOP, text='{"x":1}', parsed=parsed
    )
    extractor = _make_extractor(response)
    envelope = await extractor._call_api("prompt")
    assert isinstance(envelope, ExtractionCall)
    # PR1-a: parse_extraction で Signal に詰め替えられる
    # (parsed と同一 instance ではない)
    assert isinstance(envelope.result, Signal)
    assert envelope.result.title_ja == "t"
    assert envelope.raw_response == '{"x":1}'
    assert envelope.raw_relevance == "signal"
    assert envelope.prompt_version == GeminiExtractionPrompt.VERSION
    assert envelope.model_name == GeminiExtractionPrompt.MODEL


@pytest.mark.asyncio
async def test_max_tokens_without_parsed_raises_response_invalid() -> None:
    """policy block 系以外で parsed が GeminiExtractionResponse でない場合は
    Layer 2-B。"""
    response = _make_response(finish_reason=FinishReason.MAX_TOKENS, text="truncated")
    extractor = _make_extractor(response)
    with pytest.raises(ExtractionResponseInvalidError) as ei:
        await extractor._call_api("prompt")
    assert ei.value.CODE == "extraction_response_invalid"
