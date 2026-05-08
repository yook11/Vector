"""``GeminiExtractor._call_api`` の finish_reason 検査テスト (PR3-a-1)。

検証する性質:
- finish_reason が SAFETY/RECITATION/BLOCKLIST/PROHIBITED_CONTENT/SPII の
  いずれかなら ``ExtractionPolicyBlockedError`` を raise する
- ``raw_response`` (もし text があれば) と ``prompt_version`` を例外に運ぶ
- ``finish_reason=STOP`` (通常終了) で ``parsed`` が ExtractionResult なら
  ``ExtractionCall`` を返す
- ``finish_reason=MAX_TOKENS`` のように policy block 系 **以外** で
  ``parsed`` が ExtractionResult でない場合は ``ProviderError``
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

from app.analysis.domain.value_objects.entity import EntityRawType, EntitySurface
from app.analysis.errors import ProviderError
from app.analysis.extraction.domain import ExtractedEntity, ExtractionResult
from app.analysis.extraction.extractor.envelope import ExtractionCall
from app.analysis.extraction.extractor.errors import ExtractionPolicyBlockedError
from app.analysis.extraction.extractor.gemini import GeminiExtractor
from app.analysis.extraction.extractor.gemini_prompt import GeminiExtractionPrompt


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


def _ok_extraction_result() -> ExtractionResult:
    return ExtractionResult(
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
async def test_policy_block_finish_reason_raises_blocked_error(
    blocked_reason: FinishReason,
) -> None:
    response = _make_response(finish_reason=blocked_reason, text="some draft")
    extractor = _make_extractor(response)
    with pytest.raises(ExtractionPolicyBlockedError) as ei:
        await extractor._call_api("prompt")
    assert ei.value.finish_reason == blocked_reason.name
    assert ei.value.raw_response == "some draft"
    assert ei.value.prompt_version == GeminiExtractionPrompt.VERSION


@pytest.mark.asyncio
async def test_policy_block_with_no_text_keeps_raw_response_none() -> None:
    response = _make_response(finish_reason=FinishReason.SAFETY, text="")
    extractor = _make_extractor(response)
    with pytest.raises(ExtractionPolicyBlockedError) as ei:
        await extractor._call_api("prompt")
    assert ei.value.raw_response is None


@pytest.mark.asyncio
async def test_stop_with_parsed_result_returns_envelope() -> None:
    parsed = _ok_extraction_result()
    response = _make_response(
        finish_reason=FinishReason.STOP, text='{"x":1}', parsed=parsed
    )
    extractor = _make_extractor(response)
    envelope = await extractor._call_api("prompt")
    assert isinstance(envelope, ExtractionCall)
    assert envelope.result is parsed
    assert envelope.raw_response == '{"x":1}'
    assert envelope.prompt_version == GeminiExtractionPrompt.VERSION


@pytest.mark.asyncio
async def test_max_tokens_without_parsed_raises_provider_error() -> None:
    response = _make_response(finish_reason=FinishReason.MAX_TOKENS, text="truncated")
    extractor = _make_extractor(response)
    with pytest.raises(ProviderError):
        await extractor._call_api("prompt")
