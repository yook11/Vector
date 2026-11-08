"""Geminiの応答の読み取り。実SDKの応答型を組み立てて直接渡す(通信なし)。"""

from __future__ import annotations

import json

import pytest
from google.genai.types import (
    BlockedReason,
    Candidate,
    Content,
    FinishReason,
    GenerateContentResponse,
    GenerateContentResponsePromptFeedback,
    Part,
)

from app.ai_providers.errors import AIProviderResultError, AIProviderResultReason
from app.ai_providers.gemini.response import (
    GeminiStreamReader,
    read_gemini_json_object,
)

_VALID_JSON = json.dumps({"result": "accepted", "tags": ["sdk"]})


def _response(
    text: str = _VALID_JSON,
    *,
    finish_reason: FinishReason | None = FinishReason.STOP,
    block_reason: BlockedReason | None = None,
) -> GenerateContentResponse:
    return GenerateContentResponse(
        candidates=[
            Candidate(
                finish_reason=finish_reason,
                content=Content(role="model", parts=[Part(text=text)]),
            )
        ],
        prompt_feedback=(
            None
            if block_reason is None
            else GenerateContentResponsePromptFeedback(block_reason=block_reason)
        ),
    )


def test_stop_response_returns_the_json_object() -> None:
    """STOPで終わった応答は、本文のJSON objectを返す。"""
    assert read_gemini_json_object(_response()) == {
        "result": "accepted",
        "tags": ["sdk"],
    }


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("MODEL_OUTPUT_SENTINEL_NOT_JSON", id="not-json"),
        pytest.param(json.dumps(["MODEL_OUTPUT_SENTINEL_NOT_OBJECT"]), id="not-object"),
    ],
)
def test_unreadable_text_becomes_unparseable_without_model_output(text: str) -> None:
    """JSON objectとして読めない本文は、中身を含まない結果の失敗にする。"""
    with pytest.raises(AIProviderResultError) as exc_info:
        read_gemini_json_object(_response(text))

    assert exc_info.value.reason is AIProviderResultReason.RESPONSE_UNPARSEABLE
    assert "MODEL_OUTPUT_SENTINEL" not in str(exc_info.value)
    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None


@pytest.mark.parametrize(
    ("finish_reason", "expected_reason"),
    [
        (FinishReason.SAFETY, AIProviderResultReason.OUTPUT_BLOCKED_SAFETY),
        (FinishReason.RECITATION, AIProviderResultReason.OUTPUT_BLOCKED_RECITATION),
        (FinishReason.BLOCKLIST, AIProviderResultReason.OUTPUT_BLOCKED_BLOCKLIST),
        (
            FinishReason.PROHIBITED_CONTENT,
            AIProviderResultReason.OUTPUT_BLOCKED_PROHIBITED_CONTENT,
        ),
        (FinishReason.SPII, AIProviderResultReason.OUTPUT_BLOCKED_SPII),
    ],
)
def test_blocked_finish_reason_becomes_output_blocked(
    finish_reason: FinishReason,
    expected_reason: AIProviderResultReason,
) -> None:
    """出力を止めた終わりの理由は、本文が読めても出力のブロックにする。"""
    with pytest.raises(AIProviderResultError) as exc_info:
        read_gemini_json_object(_response(finish_reason=finish_reason))

    assert exc_info.value.reason is expected_reason


def test_max_tokens_is_truncated_even_with_valid_json() -> None:
    """MAX_TOKENSは、本文がJSONとして読めても打ち切りにする。"""
    with pytest.raises(AIProviderResultError) as exc_info:
        read_gemini_json_object(_response(finish_reason=FinishReason.MAX_TOKENS))

    assert exc_info.value.reason is AIProviderResultReason.OUTPUT_TRUNCATED


def test_prompt_block_precedes_candidate_safety() -> None:
    """入力のブロックは、候補の終わりの理由より先に判定する。"""
    response = _response(
        finish_reason=FinishReason.SAFETY,
        block_reason=BlockedReason.SAFETY,
    )

    with pytest.raises(AIProviderResultError) as exc_info:
        read_gemini_json_object(response)

    assert exc_info.value.reason is AIProviderResultReason.INPUT_BLOCKED


def test_stream_reader_returns_each_fragment_and_accepts_a_finished_stream() -> None:
    """streamの断片の本文を順に返し、終わりの理由を受け取ったstreamは失敗にしない。"""
    reader = GeminiStreamReader()

    fragments = [
        reader.read(_response("前半", finish_reason=None)),
        reader.read(_response("後半", finish_reason=FinishReason.STOP)),
    ]
    reader.finish()

    assert fragments == ["前半", "後半"]


def test_stream_reader_without_finish_reason_is_incomplete() -> None:
    """終わりの理由を受け取らずに尽きたstreamは、途切れた失敗にする。"""
    reader = GeminiStreamReader()
    reader.read(_response("途中まで", finish_reason=None))

    with pytest.raises(AIProviderResultError) as exc_info:
        reader.finish()

    assert exc_info.value.reason is AIProviderResultReason.STREAM_INCOMPLETE


@pytest.mark.parametrize(
    ("chunk", "expected_reason"),
    [
        pytest.param(
            _response(finish_reason=None, block_reason=BlockedReason.SAFETY),
            AIProviderResultReason.INPUT_BLOCKED,
            id="prompt-block",
        ),
        pytest.param(
            _response(finish_reason=FinishReason.SAFETY),
            AIProviderResultReason.OUTPUT_BLOCKED_SAFETY,
            id="output-block",
        ),
        pytest.param(
            _response(finish_reason=FinishReason.MAX_TOKENS),
            AIProviderResultReason.OUTPUT_TRUNCATED,
            id="truncated",
        ),
    ],
)
def test_stream_reader_fails_on_blocked_or_truncated_chunk(
    chunk: GenerateContentResponse,
    expected_reason: AIProviderResultReason,
) -> None:
    """ブロックか打ち切りの断片は、本文を返さずに失敗にする。"""
    with pytest.raises(AIProviderResultError) as exc_info:
        GeminiStreamReader().read(chunk)

    assert exc_info.value.reason is expected_reason
