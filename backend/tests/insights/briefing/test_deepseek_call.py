"""DeepSeekBriefingGenerator.generate の API 呼出引数検証 (mock)。

実 LLM 呼出はテストせず、SDK の chat.completions.create に渡す引数構造を
スナップショット的に検証する。特に ``extra_body={"thinking": {"type":
"disabled"}}`` は Pro モデル + tool_choice + strict の組合せで 400 を防ぐ
必須パラメータなので回帰検証する
(`feedback_deepseek_pro_thinking_disabled.md`)。
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import openai
import pytest
from openai.types.chat.chat_completion_message_function_tool_call import (
    ChatCompletionMessageFunctionToolCall,
    Function,
)
from pydantic import SecretStr

from app.insights.briefing.domain.article import ArticleInput
from app.insights.briefing.domain.briefing import (
    MAX_CHAPTERS_PER_BRIEFING,
    MAX_KEY_ARTICLE_SIGNIFICANCE_LEN,
    MAX_KEY_ARTICLES_PER_BRIEFING,
)
from app.insights.briefing.errors import (
    BriefingLlmError,
    BriefingLlmResponseInvalidError,
)


def _fake_completion_with_tool_call(arguments: dict) -> MagicMock:
    """tool_call.function.arguments を返す chat.completions レスポンス mock。

    generate() は SDK union を ``ChatCompletionMessageFunctionToolCall`` へ
    isinstance narrow するため、実体の function tool call を渡す。
    """
    tool_call = ChatCompletionMessageFunctionToolCall(
        id="call_briefing",
        type="function",
        function=Function(
            name="submit_weekly_briefing", arguments=json.dumps(arguments)
        ),
    )
    choice = MagicMock()
    choice.message.tool_calls = [tool_call]
    choice.finish_reason = "tool_calls"
    resp = MagicMock()
    resp.choices = [choice]
    return resp


@pytest.mark.asyncio
async def test_disables_thinking_for_pro_model() -> None:
    """tool_choice + strict と衝突しないよう thinking を明示無効化して呼ぶ。"""
    create = AsyncMock(
        return_value=_fake_completion_with_tool_call(
            {
                "headline": "h",
                "summary": "s",
                "chapters": [{"heading": "見出し", "body": "本文"}],
                "key_articles": [{"article_id": 1, "significance": "なぜ重要か"}],
                "watch_points": [{"statement": "今後どこを見るべきか"}],
            }
        )
    )

    fake_client = MagicMock()
    fake_client.chat.completions.create = create

    with (
        patch("app.insights.briefing.llm.settings") as mock_settings,
        patch("app.insights.briefing.llm.AsyncOpenAI", return_value=fake_client),
    ):
        mock_settings.deepseek_api_key = SecretStr("test-key")
        from app.insights.briefing.llm import DeepSeekBriefingGenerator

        gen = DeepSeekBriefingGenerator()
        result = await gen.generate(
            category_name="AI",
            week_start=date(2026, 4, 20),
            articles=[ArticleInput(id=1, title_ja="t", summary_ja="s")],
        )

    create.assert_awaited_once()
    kwargs = create.await_args.kwargs
    assert kwargs["model"] == "deepseek-v4-pro"
    assert kwargs["tool_choice"]["function"]["name"] == "submit_weekly_briefing"
    assert kwargs["tools"][0]["function"]["strict"] is True
    # 回帰防止: thinking モード明示無効化が抜けると Pro で 400 になる
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    # 新 schema が parse されている
    assert result.summary == "s"
    assert result.chapters[0].heading == "見出し"
    assert result.chapters[0].body == "本文"
    assert result.key_articles[0].article_id == 1
    assert result.key_articles[0].significance == "なぜ重要か"
    assert result.watch_points[0].statement == "今後どこを見るべきか"


def test_tool_schema_required_fields_match_new_output() -> None:
    """tool schema 側でも新 5 field 構造が要求されていることを保証する。"""
    from app.insights.briefing.llm import BRIEFING_TOOL_SCHEMA

    assert BRIEFING_TOOL_SCHEMA["required"] == [
        "headline",
        "summary",
        "chapters",
        "key_articles",
        "watch_points",
    ]
    chapter = BRIEFING_TOOL_SCHEMA["properties"]["chapters"]["items"]
    assert chapter["required"] == ["heading", "body"]
    key_article = BRIEFING_TOOL_SCHEMA["properties"]["key_articles"]["items"]
    assert key_article["required"] == ["article_id", "significance"]
    watch_point = BRIEFING_TOOL_SCHEMA["properties"]["watch_points"]["items"]
    assert watch_point["required"] == ["statement"]
    # 旧 overview / stories 構造の名残が残っていないこと
    assert "overview" not in BRIEFING_TOOL_SCHEMA["properties"]
    assert "stories" not in BRIEFING_TOOL_SCHEMA["properties"]


async def _generate_with_mocked_response(arguments: dict) -> None:
    """LLM 出力 mock で generate を呼ぶ共通ヘルパ (F10 振る舞い test 用)。"""
    create = AsyncMock(return_value=_fake_completion_with_tool_call(arguments))
    fake_client = MagicMock()
    fake_client.chat.completions.create = create

    with (
        patch("app.insights.briefing.llm.settings") as mock_settings,
        patch("app.insights.briefing.llm.AsyncOpenAI", return_value=fake_client),
    ):
        mock_settings.deepseek_api_key = SecretStr("test-key")
        from app.insights.briefing.llm import DeepSeekBriefingGenerator

        gen = DeepSeekBriefingGenerator()
        await gen.generate(
            category_name="AI",
            week_start=date(2026, 4, 20),
            articles=[ArticleInput(id=1, title_ja="t", summary_ja="s")],
        )


@pytest.mark.asyncio
async def test_generator_rejects_abnormal_key_article_count_from_llm() -> None:
    """LLM が F10 異常検知ライン超の key_articles を返したら marker に wrap する。

    AUTH-N4/AUTH-C1 を持たない LLM 暴走 / prompt injection シナリオでも、
    domain VO の Field(max_length=MAX_KEY_ARTICLES_PER_BRIEFING) で巨大 briefing
    が DB に入る前に reject される (red-team F10 二次防衛)。
    violations に "key_articles" を含む自己記述化メッセージが焼かれることで
    どの field が上限超だったかが audit から直接読める。
    """
    oversized = {
        "headline": "h",
        "summary": "s",
        "chapters": [{"heading": "見出し", "body": "本文"}],
        "key_articles": [
            {"article_id": i, "significance": f"s{i}"}
            for i in range(MAX_KEY_ARTICLES_PER_BRIEFING + 1)
        ],
        "watch_points": [{"statement": "w"}],
    }
    with pytest.raises(BriefingLlmResponseInvalidError) as raised:
        await _generate_with_mocked_response(oversized)

    exc = raised.value
    # violations に "key_articles" フィールドの制約種別が入っている
    assert any("key_articles" in v for v in exc.violations)
    # str(exc) も CODE + violations 形式になっている
    assert exc.CODE in str(exc)
    assert any("key_articles" in v for v in str(exc).split(exc.CODE + ": ", 1)[-1].split("; "))


@pytest.mark.asyncio
async def test_generator_rejects_abnormal_chapter_count_from_llm() -> None:
    """LLM が上限ガード超の chapters を返したら briefing marker に wrap する。"""
    oversized = {
        "headline": "h",
        "summary": "s",
        "chapters": [
            {"heading": f"h{i}", "body": f"b{i}"}
            for i in range(MAX_CHAPTERS_PER_BRIEFING + 1)
        ],
        "key_articles": [{"article_id": 1, "significance": "s"}],
        "watch_points": [{"statement": "w"}],
    }
    with pytest.raises(BriefingLlmResponseInvalidError):
        await _generate_with_mocked_response(oversized)


@pytest.mark.asyncio
async def test_generator_rejects_oversize_significance_from_llm() -> None:
    """LLM が上限超の significance を返したら briefing marker に wrap する。

    violations は「loc: 制約種別」の value-free な形式で焼かれる。
    超過した significance 文字列本文 (untrusted content) は violations に
    含まれず、audit / log へ LLM 出力が素通りしないことを保証する。
    """
    oversized_significance = "x" * (MAX_KEY_ARTICLE_SIGNIFICANCE_LEN + 1)
    oversized = {
        "headline": "h",
        "summary": "s",
        "chapters": [{"heading": "見出し", "body": "本文"}],
        "key_articles": [
            {
                "article_id": 1,
                "significance": oversized_significance,
            }
        ],
        "watch_points": [{"statement": "w"}],
    }
    with pytest.raises(BriefingLlmResponseInvalidError) as raised:
        await _generate_with_mocked_response(oversized)

    exc = raised.value
    # violations に significance フィールドの制約種別が入っている
    assert any("significance" in v for v in exc.violations)
    # LLM 出力の本文値 (超過文字列) は violations に含まれない (value-free 保証)
    assert not any(oversized_significance in v for v in exc.violations)


@pytest.mark.asyncio
async def test_generator_wraps_openai_api_error() -> None:
    """OpenAI SDK 例外は briefing marker に wrap して stage 境界へ出す。"""
    request = httpx.Request("POST", "https://api.deepseek.com/beta/chat/completions")
    provider_error = openai.APIError("upstream", request=request, body=None)
    create = AsyncMock(side_effect=provider_error)
    fake_client = MagicMock()
    fake_client.chat.completions.create = create

    with (
        patch("app.insights.briefing.llm.settings") as mock_settings,
        patch("app.insights.briefing.llm.AsyncOpenAI", return_value=fake_client),
    ):
        mock_settings.deepseek_api_key = SecretStr("test-key")
        from app.insights.briefing.llm import DeepSeekBriefingGenerator

        gen = DeepSeekBriefingGenerator()
        with pytest.raises(BriefingLlmError) as raised:
            await gen.generate(
                category_name="AI",
                week_start=date(2026, 4, 20),
                articles=[ArticleInput(id=1, title_ja="t", summary_ja="s")],
            )

    assert raised.value.provider_error is provider_error
    assert raised.value.__cause__ is provider_error
