"""``AnalyzableArticle`` — 分析工程に進める記事 (collection BC の出口契約)。

``id`` を持たない (identity は永続化後の関心事)。長さ境界の SSoT は
``article_limits``。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.collection.domain.article_limits import (
    ARTICLE_BODY_MAX_LENGTH,
    ARTICLE_BODY_MIN_LENGTH,
    ARTICLE_TITLE_MAX_LENGTH,
    ARTICLE_TITLE_MIN_LENGTH,
)
from app.collection.domain.article_url import ArticleUrl
from app.collection.domain.value_objects import PublishedAt


class AnalyzableArticleDefect(StrEnum):
    """分析可能な記事の条件違反を、監査にも使う自己記述コードで表す。"""

    TITLE_MISSING = "analyzable_article_title_missing"
    TITLE_TOO_SHORT = "analyzable_article_title_too_short"
    TITLE_TOO_LONG = "analyzable_article_title_too_long"
    BODY_MISSING = "analyzable_article_body_missing"
    BODY_TOO_SHORT = "analyzable_article_body_too_short"
    BODY_TOO_LONG = "analyzable_article_body_too_long"
    PUBLISHED_AT_MISSING = "analyzable_article_published_at_missing"
    SOURCE_ID_INVALID = "analyzable_article_source_id_invalid"


_DEFECT_BY_LOC_TYPE: dict[tuple[str, str], AnalyzableArticleDefect] = {
    ("title", "string_type"): AnalyzableArticleDefect.TITLE_MISSING,
    ("title", "string_too_short"): AnalyzableArticleDefect.TITLE_TOO_SHORT,
    ("title", "string_too_long"): AnalyzableArticleDefect.TITLE_TOO_LONG,
    ("body", "string_type"): AnalyzableArticleDefect.BODY_MISSING,
    ("body", "string_too_short"): AnalyzableArticleDefect.BODY_TOO_SHORT,
    ("body", "string_too_long"): AnalyzableArticleDefect.BODY_TOO_LONG,
    ("published_at", "dataclass_type"): AnalyzableArticleDefect.PUBLISHED_AT_MISSING,
    ("source_id", "greater_than"): AnalyzableArticleDefect.SOURCE_ID_INVALID,
}


def _classify_defects(
    exc: ValidationError,
) -> tuple[AnalyzableArticleDefect, ...] | None:
    """全件が既知の条件違反である場合だけ、構築拒否の理由として返す。"""
    defects: list[AnalyzableArticleDefect] = []
    for err in exc.errors(include_input=False):
        loc = err["loc"]
        field = str(loc[0]) if loc else ""
        defect = _DEFECT_BY_LOC_TYPE.get((field, err["type"]))
        if defect is None:
            return None
        defects.append(defect)
    return tuple(defects) or None


@dataclass(frozen=True, slots=True)
class QualityTooLow:
    """構築拒否が確定した場合だけ使い、未分類の検証失敗は含めない。"""

    defects: tuple[AnalyzableArticleDefect, ...]

    def __post_init__(self) -> None:
        if not self.defects:
            raise ValueError("記事の構築拒否には理由が必要です")


class AnalyzableArticle(BaseModel):
    """分析工程に進める記事。

    不変条件 (Field で保証):
    - ``title``: 1..500 文字
    - ``body``: 50..200_000 文字
    - ``published_at``: 必須
    - ``source_id`` / ``source_url``: 原産情報 (UNIQUE 衝突判定 / 監査に必須)
    - frozen: 生成後は不変
    """

    model_config = ConfigDict(frozen=True)

    title: str = Field(
        min_length=ARTICLE_TITLE_MIN_LENGTH, max_length=ARTICLE_TITLE_MAX_LENGTH
    )
    body: str = Field(
        min_length=ARTICLE_BODY_MIN_LENGTH, max_length=ARTICLE_BODY_MAX_LENGTH
    )
    published_at: PublishedAt
    source_id: int = Field(gt=0)
    source_url: ArticleUrl

    @classmethod
    def try_build(
        cls,
        *,
        title: str,
        body: str | None,
        published_at: PublishedAt | None,
        source_id: int,
        source_url: ArticleUrl,
    ) -> Self | None:
        """素材から不変条件を満たすときのみ ``AnalyzableArticle`` を構築する。

        不変条件 (title 長 / body 長 / published 存在 / source_id > 0) の判定は
        Field constraint で構造的に保証されている。本 factory は失敗を値化して
        呼び出し側が Observed fallback などの通常分岐を if で書けるようにする。

        厳格コンストラクタ ``AnalyzableArticle(...)`` の型契約 (body /
        published_at 必須) は不変。``try_build`` は素材を Optional で受ける
        smart constructor。
        """
        if body is None or published_at is None:
            return None
        try:
            return cls(
                title=title,
                body=body,
                published_at=published_at,
                source_id=source_id,
                source_url=source_url,
            )
        except ValueError:
            return None

    @classmethod
    def build_or_reject(
        cls,
        *,
        title: str | None,
        body: str | None,
        published_at: PublishedAt | None,
        source_id: int,
        source_url: ArticleUrl,
    ) -> Self | QualityTooLow:
        """既知の条件違反は構築拒否として返し、未分類の検証失敗は元の例外を伝播する。"""
        try:
            return cls.model_validate(
                {
                    "title": title,
                    "body": body,
                    "published_at": published_at,
                    "source_id": source_id,
                    "source_url": source_url,
                }
            )
        except ValidationError as exc:
            defects = _classify_defects(exc)
            if defects is None:
                raise
            return QualityTooLow(defects=defects)
