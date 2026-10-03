"""GeminiBriefingGenerator.generate の API 呼出と応答分類の検証 (mock)。

実 LLM 呼出はテストせず、SDK の ``models.generate_content`` に渡す引数構造と、
応答を briefing の結果・stage marker 例外へ振り分ける境界を検証する。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from google.genai import errors as genai_errors
from google.genai.types import (
    BlockedReason,
    Candidate,
    Content,
    FinishReason,
    GenerateContentResponse,
    GenerateContentResponsePromptFeedback,
    Part,
    ThinkingLevel,
)

from app.ai_providers.errors import (
    AIProviderResponseError,
    AIProviderResultError,
    AIProviderResultReason,
    AIProviderTransportError,
)
from app.insights.briefing.domain.briefing import (
    MAX_CHAPTERS_PER_BRIEFING,
    MAX_KEY_ARTICLE_SIGNIFICANCE_LEN,
    MAX_KEY_ARTICLES_PER_BRIEFING,
)
from app.insights.briefing.domain.ready import BriefingArticle
from app.insights.briefing.errors import (
    BriefingLlmError,
    BriefingLlmResponseInvalidError,
)
from app.insights.briefing.llm import BRIEFING_GEMINI_SCHEMA, GeminiBriefingGenerator


class _ClientScope:
    """generate_content を差し替えた client を貸し、開閉を記録する。"""

    def __init__(self, generate_content: AsyncMock) -> None:
        self.client = MagicMock()
        self.client.models.generate_content = generate_content
        self.events: list[str] = []

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[MagicMock]:
        self.events.append("open")
        try:
            yield self.client
        finally:
            self.events.append("close")


_ARTICLES = [BriefingArticle(analyzed_article_id=1, translated_title="t", summary="s")]

_VALID_PAYLOAD = {
    "headline": "h",
    "summary": "s",
    "chapters": [{"heading": "見出し", "body": "本文"}],
    "key_articles": [{"analyzed_article_id": 1, "significance": "なぜ重要か"}],
    "watch_points": [{"statement": "今後どこを見るべきか"}],
}


def _response(
    text: str | None,
    *,
    finish_reason: FinishReason | None = FinishReason.STOP,
    prompt_block: bool = False,
) -> GenerateContentResponse:
    candidate = Candidate(
        finish_reason=finish_reason,
        content=Content(role="model", parts=[Part(text=text)]) if text else None,
    )
    return GenerateContentResponse(
        candidates=[] if prompt_block else [candidate],
        prompt_feedback=GenerateContentResponsePromptFeedback(
            block_reason=BlockedReason.SAFETY
        )
        if prompt_block
        else None,
    )


async def _generate(response_or_error: object) -> None:
    mock = (
        AsyncMock(side_effect=response_or_error)
        if isinstance(response_or_error, BaseException)
        else AsyncMock(return_value=response_or_error)
    )
    gen = GeminiBriefingGenerator(client_scope_factory=_ClientScope(mock))
    await gen.generate(
        category_name="AI", week_start=date(2026, 4, 20), articles=_ARTICLES
    )


@pytest.mark.asyncio
async def test_requests_declared_model_with_high_thinking_and_parses_result() -> None:
    """3.8 Flash を thinking high・JSON 出力で呼び、応答を briefing に復元する。"""
    generate_content = AsyncMock(return_value=_response(json.dumps(_VALID_PAYLOAD)))
    gen = GeminiBriefingGenerator(client_scope_factory=_ClientScope(generate_content))

    result = await gen.generate(
        category_name="AI", week_start=date(2026, 4, 20), articles=_ARTICLES
    )

    kwargs = generate_content.await_args.kwargs
    config = kwargs["config"]
    assert kwargs["model"] == "gemini-3.8-flash"
    assert config.thinking_config.thinking_level is ThinkingLevel.HIGH
    assert config.max_output_tokens == 32768
    assert config.response_mime_type == "application/json"
    assert config.response_schema == BRIEFING_GEMINI_SCHEMA
    assert config.temperature is None
    assert "analyzed_article_id: 1" in kwargs["contents"]
    assert result.summary == "s"
    assert result.chapters[0].heading == "見出し"
    assert result.chapters[0].body == "本文"
    assert result.key_articles[0].analyzed_article_id == 1
    assert result.key_articles[0].significance == "なぜ重要か"
    assert result.watch_points[0].statement == "今後どこを見るべきか"


def test_schema_requires_the_five_briefing_fields() -> None:
    """schema 側でも 5 field 構造が要求されていることを保証する。"""
    assert BRIEFING_GEMINI_SCHEMA["required"] == [
        "headline",
        "summary",
        "chapters",
        "key_articles",
        "watch_points",
    ]
    chapter = BRIEFING_GEMINI_SCHEMA["properties"]["chapters"]["items"]
    assert chapter["required"] == ["heading", "body"]
    key_article = BRIEFING_GEMINI_SCHEMA["properties"]["key_articles"]["items"]
    assert key_article["required"] == ["analyzed_article_id", "significance"]
    watch_point = BRIEFING_GEMINI_SCHEMA["properties"]["watch_points"]["items"]
    assert watch_point["required"] == ["statement"]


@pytest.mark.asyncio
async def test_generator_rejects_abnormal_key_article_count_from_llm() -> None:
    """LLM が F10 異常検知ライン超の key_articles を返したら marker に wrap する。

    domain VO の Field(max_length=MAX_KEY_ARTICLES_PER_BRIEFING) で巨大 briefing
    が DB に入る前に reject される (red-team F10 二次防衛)。violations に
    "key_articles" を含む自己記述化メッセージが焼かれ、audit から直接読める。
    """
    oversized = {
        **_VALID_PAYLOAD,
        "key_articles": [
            {"analyzed_article_id": i, "significance": f"s{i}"}
            for i in range(MAX_KEY_ARTICLES_PER_BRIEFING + 1)
        ],
    }
    with pytest.raises(BriefingLlmResponseInvalidError) as raised:
        await _generate(_response(json.dumps(oversized)))

    exc = raised.value
    assert any("key_articles" in v for v in exc.violations)
    assert exc.CODE in str(exc)


@pytest.mark.asyncio
async def test_generator_rejects_abnormal_chapter_count_from_llm() -> None:
    """LLM が上限ガード超の chapters を返したら briefing marker に wrap する。"""
    oversized = {
        **_VALID_PAYLOAD,
        "chapters": [
            {"heading": f"h{i}", "body": f"b{i}"}
            for i in range(MAX_CHAPTERS_PER_BRIEFING + 1)
        ],
    }
    with pytest.raises(BriefingLlmResponseInvalidError):
        await _generate(_response(json.dumps(oversized)))


@pytest.mark.asyncio
async def test_generator_rejects_oversize_significance_without_echoing_it() -> None:
    """上限超の significance は violations に値を含めずに marker へ wrap する。"""
    oversized_significance = "x" * (MAX_KEY_ARTICLE_SIGNIFICANCE_LEN + 1)
    oversized = {
        **_VALID_PAYLOAD,
        "key_articles": [
            {"analyzed_article_id": 1, "significance": oversized_significance}
        ],
    }
    with pytest.raises(BriefingLlmResponseInvalidError) as raised:
        await _generate(_response(json.dumps(oversized)))

    exc = raised.value
    assert any("significance" in v for v in exc.violations)
    assert not any(oversized_significance in v for v in exc.violations)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["PRIVATE_NOT_JSON", None])
async def test_unreadable_body_is_a_response_contract_violation(text) -> None:
    """JSON として読めない本文 (空を含む) は値を含めずに応答不正とする。"""
    with pytest.raises(BriefingLlmResponseInvalidError) as raised:
        await _generate(_response(text))

    assert raised.value.violations
    assert "PRIVATE_" not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            _response(None, prompt_block=True),
            AIProviderResultReason.INPUT_BLOCKED,
        ),
        (
            _response(json.dumps(_VALID_PAYLOAD), finish_reason=FinishReason.SAFETY),
            AIProviderResultReason.OUTPUT_BLOCKED_SAFETY,
        ),
        (
            _response(
                json.dumps(_VALID_PAYLOAD), finish_reason=FinishReason.MAX_TOKENS
            ),
            AIProviderResultReason.OUTPUT_TRUNCATED,
        ),
    ],
)
async def test_unusable_result_is_wrapped_before_body_validation(
    response, reason
) -> None:
    """入力ブロック・出力拒否・打ち切りは本文が読めても AI の失敗として wrap する。"""
    with pytest.raises(BriefingLlmError) as raised:
        await _generate(response)

    provider_error = raised.value.provider_error
    assert isinstance(provider_error, AIProviderResultError)
    assert provider_error.reason is reason


@pytest.mark.asyncio
async def test_generator_wraps_classified_sdk_error() -> None:
    """分類できる SDK 例外は AI の例外にして briefing marker に wrap する。"""
    error = genai_errors.ServerError(
        500, {"error": {"status": "INTERNAL", "message": "upstream"}}
    )

    with pytest.raises(BriefingLlmError) as raised:
        await _generate(error)

    assert isinstance(raised.value.provider_error, AIProviderResponseError)
    assert raised.value.__cause__ is error


@pytest.mark.asyncio
async def test_generator_wraps_transport_error() -> None:
    """通信の失敗も AI の例外にして briefing marker に wrap する。"""
    error = httpx.ReadTimeout("read timeout")

    with pytest.raises(BriefingLlmError) as raised:
        await _generate(error)

    assert isinstance(raised.value.provider_error, AIProviderTransportError)


@pytest.mark.asyncio
async def test_generator_wraps_unclassified_sdk_error_as_is() -> None:
    """分類できない SDK 例外は、そのまま provider_error として wrap する。"""
    error = genai_errors.APIError(
        418, {"error": {"status": "UNKNOWN_STATUS", "message": "teapot"}}
    )

    with pytest.raises(BriefingLlmError) as raised:
        await _generate(error)

    assert raised.value.provider_error is error


@pytest.mark.asyncio
async def test_non_sdk_error_is_not_wrapped() -> None:
    """SDK 外の想定外例外は briefing marker に包まずそのまま伝える。"""
    error = RuntimeError("unexpected")

    with pytest.raises(RuntimeError) as raised:
        await _generate(error)

    assert raised.value is error


@pytest.mark.asyncio
async def test_generate_opens_and_closes_client_for_each_call() -> None:
    """client は生成のたびに開いて閉じ、呼び出しをまたいで持ち越さない。"""
    scope = _ClientScope(AsyncMock(return_value=_response(json.dumps(_VALID_PAYLOAD))))
    gen = GeminiBriefingGenerator(client_scope_factory=scope)

    for _ in range(2):
        await gen.generate(
            category_name="AI", week_start=date(2026, 4, 20), articles=_ARTICLES
        )

    assert scope.events == ["open", "close", "open", "close"]


@pytest.mark.asyncio
async def test_generate_closes_client_when_provider_call_fails() -> None:
    """SDK の呼び出しが失敗しても client を閉じる。"""
    scope = _ClientScope(
        AsyncMock(
            side_effect=genai_errors.ServerError(
                500, {"error": {"status": "INTERNAL", "message": "upstream"}}
            )
        )
    )
    gen = GeminiBriefingGenerator(client_scope_factory=scope)

    with pytest.raises(BriefingLlmError):
        await gen.generate(
            category_name="AI", week_start=date(2026, 4, 20), articles=_ARTICLES
        )

    assert scope.events == ["open", "close"]
