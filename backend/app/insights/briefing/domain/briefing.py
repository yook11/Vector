"""Briefing の LLM 出力 VO + ハルシネーション検証。

DeepSeek-V4 Pro が返す JSON を ``WeeklyBriefingContent`` として受ける。
``article_ids`` が ``input_ids`` の subset であることを ``model_validator``
(mode="after") で検証し、捏造記事 id を含む応答を構造的に弾く。

検証 context:
    ``WeeklyBriefingContent.model_validate(data, context={"input_ids": {1, 2, 3}})``
    のように context 経由で input_ids を渡す。LLM 呼出側 (DeepSeekBriefingGenerator)
    の責務。
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator


class BriefingStory(BaseModel):
    """1 ストーリー = ある業界トレンドの題目 + 分析 + 根拠記事 ids。"""

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1)
    analysis: str = Field(min_length=1)
    article_ids: list[int] = Field(min_length=1)


class WeeklyBriefingContent(BaseModel):
    """LLM が返す 1 カテゴリ × 1 週分の briefing 全体。"""

    model_config = ConfigDict(frozen=True)

    headline: str = Field(min_length=1)
    stories: list[BriefingStory] = Field(min_length=1)

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
