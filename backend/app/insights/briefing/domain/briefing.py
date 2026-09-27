"""briefing の成果物 (LLM 出力を検証した内容) の VO。

LLM 応答の VO 化は ``WeeklyBriefingContent.from_llm_payload`` だけを本番の入口にし、
``input_ids`` を必須にして捏造記事 id の検証漏れを防ぐ。各上限は閲覧 API の
response schema (``briefing/schemas.py``) にも同値で持つ
(閲覧側はこの VO を通らないため)。
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

MAX_BRIEFING_HEADLINE_LEN: Final[int] = 200
MAX_BRIEFING_SUMMARY_LEN: Final[int] = 1_000
MAX_CHAPTER_HEADING_LEN: Final[int] = 80
MAX_CHAPTER_BODY_LEN: Final[int] = 3_000
# 章数の上限ガード。章数は LLM 裁量 (下限 1) で、これは破綻防止の異常検知ライン。
MAX_CHAPTERS_PER_BRIEFING: Final[int] = 12
MAX_KEY_ARTICLE_SIGNIFICANCE_LEN: Final[int] = 600
# editorial 上限 (プロンプトの「最大 5 件」) とは別物の F10 異常検知ライン。
# 7 件程度の正常なばらつきは通し、injection / 暴走を疑う件数だけ loud に弾く。
MAX_KEY_ARTICLES_PER_BRIEFING: Final[int] = 20
MAX_WATCH_POINT_STATEMENT_LEN: Final[int] = 600
# watch_points も editorial (プロンプトの「1-3 件」) とは別の F10 異常検知ライン。
MAX_WATCH_POINTS_PER_BRIEFING: Final[int] = 8


class BriefingChapter(BaseModel):
    """週の流れを章立てにした本文の章 1 つ。"""

    model_config = ConfigDict(frozen=True)

    heading: str = Field(min_length=1, max_length=MAX_CHAPTER_HEADING_LEN)
    body: str = Field(min_length=1, max_length=MAX_CHAPTER_BODY_LEN)


class KeyArticle(BaseModel):
    """その週で特に重要な記事 1 件 (analyzed_article_id は公開記事 id と同じ空間)。"""

    model_config = ConfigDict(frozen=True)

    analyzed_article_id: int
    significance: str = Field(min_length=1, max_length=MAX_KEY_ARTICLE_SIGNIFICANCE_LEN)


class WatchPoint(BaseModel):
    """今後どこを見るべきかの論点 1 件。

    根拠記事 id を後から足せるようオブジェクト形で持つ。
    """

    model_config = ConfigDict(frozen=True)

    statement: str = Field(min_length=1, max_length=MAX_WATCH_POINT_STATEMENT_LEN)


class WeeklyBriefingContent(BaseModel):
    """LLM が返す 1 カテゴリ × 1 週分の briefing。

    検証を通った内容はそのまま保存できる。
    """

    model_config = ConfigDict(frozen=True)

    @classmethod
    def from_llm_payload(
        cls, payload: str, *, input_ids: AbstractSet[int]
    ) -> WeeklyBriefingContent:
        """schema 違反・重複 id・input_ids 外の id は ValidationError にする。"""
        return cls.model_validate_json(payload, context={"input_ids": input_ids})

    headline: str = Field(min_length=1, max_length=MAX_BRIEFING_HEADLINE_LEN)
    summary: str = Field(min_length=1, max_length=MAX_BRIEFING_SUMMARY_LEN)
    chapters: list[BriefingChapter] = Field(
        min_length=1, max_length=MAX_CHAPTERS_PER_BRIEFING
    )
    key_articles: list[KeyArticle] = Field(
        min_length=1, max_length=MAX_KEY_ARTICLES_PER_BRIEFING
    )
    watch_points: list[WatchPoint] = Field(
        min_length=1, max_length=MAX_WATCH_POINTS_PER_BRIEFING
    )

    @model_validator(mode="after")
    def _reject_duplicate_key_article_ids(self) -> Self:
        """同じ記事の重複掲載は契約違反で、閲覧 API の記事 id の一意性も支える。"""
        ids = [ka.analyzed_article_id for ka in self.key_articles]
        if len(ids) != len(set(ids)):
            raise ValueError(
                f"key_articles contain duplicate analyzed_article_id: {ids}"
            )
        return self

    @model_validator(mode="after")
    def _validate_article_ids_subset(self, info: ValidationInfo) -> Self:
        """context に input_ids が無ければ検証しない。

        本番経路は from_llm_payload が必ず渡す。
        """
        context = info.context
        if context is None:
            return self
        input_ids = context.get("input_ids")
        if input_ids is None:
            return self
        article_ids = {ka.analyzed_article_id for ka in self.key_articles}
        unknown = article_ids - set(input_ids)
        if unknown:
            raise ValueError(
                f"key_articles contain ids not in input_ids: {sorted(unknown)}"
            )
        return self
