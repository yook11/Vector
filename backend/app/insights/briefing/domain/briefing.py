"""Briefing の LLM 出力 VO + ハルシネーション検証。

DeepSeek-V4 Pro が返す JSON を ``WeeklyBriefingContent`` として受ける。
``article_ids`` が ``input_ids`` の subset であることを ``model_validator``
(mode="after") で検証し、捏造記事 id を含む応答を構造的に弾く。

検証 context:
    ``WeeklyBriefingContent.model_validate(data, context={"input_ids": {1, 2, 3}})``
    のように context 経由で input_ids を渡す。LLM 呼出側 (DeepSeekBriefingGenerator)
    の責務。

サイズ上限 (red-team F10 構造防御):
    各 str / list の max_length は LLM 暴走 / prompt injection で巨大 briefing が
    DB に入る経路 (二次防御) を構造的に塞ぐ。anon GET 経路の防御は
    ``schemas/briefing.py`` の response schema 側で同値の max_length を持って
    実現する (router の model_validate 経由ではないため二箇所で持つ)。
"""

from __future__ import annotations

from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

# LLM 出力の現実的な上限 (実サンプル "132 記事 → 6 stories" を基準に余裕を確保)。
# response schema (schemas/briefing.py) と同値で持ち、二箇所で同じ振る舞いを保証する。
MAX_STORY_TITLE_LEN: Final[int] = 200
MAX_STORY_ANALYSIS_LEN: Final[int] = 4_000
MAX_ARTICLE_IDS_PER_STORY: Final[int] = 50
MAX_BRIEFING_HEADLINE_LEN: Final[int] = 500
MAX_STORIES_PER_BRIEFING: Final[int] = 20


class BriefingStory(BaseModel):
    """1 ストーリー = ある業界トレンドの題目 + 分析 + 根拠記事 ids。"""

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1, max_length=MAX_STORY_TITLE_LEN)
    analysis: str = Field(min_length=1, max_length=MAX_STORY_ANALYSIS_LEN)
    article_ids: list[int] = Field(min_length=1, max_length=MAX_ARTICLE_IDS_PER_STORY)


class WeeklyBriefingContent(BaseModel):
    """LLM が返す 1 カテゴリ × 1 週分の briefing 全体。"""

    model_config = ConfigDict(frozen=True)

    headline: str = Field(min_length=1, max_length=MAX_BRIEFING_HEADLINE_LEN)
    stories: list[BriefingStory] = Field(
        min_length=1, max_length=MAX_STORIES_PER_BRIEFING
    )

    @model_validator(mode="after")
    def _validate_article_ids_subset(self, info: ValidationInfo) -> Self:
        """``article_ids ⊆ input_ids`` を構造的に保証する (ハルシネーション検出)。

        ``info.context["input_ids"]`` が指定されていない場合は検証をスキップする
        (テスト等で context を渡さない経路を許容する)。LLM 呼出経路では必ず
        ``input_ids`` を渡すこと。
        """
        context = info.context
        if context is None:
            return self
        input_ids = context.get("input_ids")
        if input_ids is None:
            return self
        all_ids: set[int] = set()
        for story in self.stories:
            all_ids.update(story.article_ids)
        unknown = all_ids - set(input_ids)
        if unknown:
            raise ValueError(
                f"article_ids contain ids not in input_ids: {sorted(unknown)}"
            )
        return self
