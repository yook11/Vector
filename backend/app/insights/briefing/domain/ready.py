"""briefing 生成を始められる状態を、取得済みの事実から I/O なしで判定する。

Ready の考え方は specs/pipeline/typed-pipeline-preconditions.md に従う。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BriefingReadyBuildRejectionReason(StrEnum):
    """生成を始めない理由。"""

    CATEGORY_MISSING = "category_missing"
    ALREADY_GENERATED = "already_generated"
    NO_ARTICLES = "no_articles"


class BriefingArticle(BaseModel):
    """briefing の素材にする分析済み記事 1 件。

    LLM には翻訳タイトルと要約だけを渡し、原文などはコストと
    プロンプトインジェクションの攻撃面を増やすため渡さない。
    """

    model_config = ConfigDict(frozen=True)

    analyzed_article_id: int = Field(gt=0)
    translated_title: str = Field(min_length=1)
    summary: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class BriefingReadyBuildFacts:
    """Ready 構築に必要な DB 射影 (カテゴリが無いときは事実そのものを None にする)。"""

    category_slug: str
    category_name: str
    already_generated: bool
    articles: tuple[BriefingArticle, ...]


def _require_monday(week_start: date) -> None:
    if week_start.weekday() != 0:
        raise ValueError(
            f"week_start must be a Monday (JST), got {week_start} "
            f"(weekday={week_start.weekday()})"
        )


class ReadyForBriefing(BaseModel):
    """生成に必要な値をそろえ、開始条件を満たした不変オブジェクト。"""

    model_config = ConfigDict(frozen=True)

    week_start: date
    category_id: int = Field(gt=0)
    category_slug: str = Field(min_length=1)
    category_name: str = Field(min_length=1)
    articles: tuple[BriefingArticle, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _ensure_monday(self) -> Self:
        _require_monday(self.week_start)
        return self

    @classmethod
    def from_facts(
        cls,
        *,
        week_start: date,
        category_id: int,
        facts: BriefingReadyBuildFacts | None,
    ) -> ReadyForBriefing | BriefingReadyBuildRejectionReason:
        """取得済みの事実から、I/O なしで開始条件を判定する。"""
        # 月曜でない週は入力の誤りなので、記事 0 件などの業務上の判定より先に弾く。
        _require_monday(week_start)
        if facts is None:
            return BriefingReadyBuildRejectionReason.CATEGORY_MISSING
        if facts.already_generated:
            return BriefingReadyBuildRejectionReason.ALREADY_GENERATED
        if not facts.articles:
            return BriefingReadyBuildRejectionReason.NO_ARTICLES
        return cls(
            week_start=week_start,
            category_id=category_id,
            category_slug=facts.category_slug,
            category_name=facts.category_name,
            articles=facts.articles,
        )
