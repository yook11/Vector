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

import pytest
from pydantic import SecretStr

from app.insights.briefing.domain.article import ArticleInput


def _fake_completion_with_tool_call(arguments: dict) -> MagicMock:
    """tool_calls[0].function.arguments を返す chat.completions レスポンス mock。"""
    tool_call = MagicMock()
    tool_call.function.name = "submit_weekly_briefing"
    tool_call.function.arguments = json.dumps(arguments)
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
                "stories": [
                    {"title": "t", "analysis": "a", "article_ids": [1]},
                ],
            }
        )
    )

    fake_client = MagicMock()
    fake_client.chat.completions.create = create

    with (
        patch("app.insights.briefing.llm.deepseek.settings") as mock_settings,
        patch(
            "app.insights.briefing.llm.deepseek.AsyncOpenAI", return_value=fake_client
        ),
    ):
        mock_settings.deepseek_api_key = SecretStr("test-key")
        from app.insights.briefing.llm.deepseek import DeepSeekBriefingGenerator

        gen = DeepSeekBriefingGenerator()
        await gen.generate(
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
