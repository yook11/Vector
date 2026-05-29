"""``AnalyzableArticle`` — 分析工程に進める記事 (collection BC の出口契約)。

``id`` を持たない (identity は永続化後の関心事)。長さ境界の SSoT は
``article_limits``。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Self

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.collection.domain.article_limits import (
    ARTICLE_BODY_MAX_LENGTH,
    ARTICLE_BODY_MIN_LENGTH,
    ARTICLE_TITLE_MAX_LENGTH,
    ARTICLE_TITLE_MIN_LENGTH,
)
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.value_objects import PublishedAt

logger = structlog.get_logger(__name__)


class AnalyzableArticleDefect(StrEnum):
    """``AnalyzableArticle`` 不変条件を満たせなかった理由 (自己記述コード)。

    value はそのまま audit の ``outcome_code`` / ``payload.defects`` に焼かれる
    (analysis BC の ready ``*ReadyBuildBlockedCode`` と同形)。stage 非依存。
    メンバーは Field 宣言順 = errors() 順に並べる (主 defect は先頭)。
    """

    TITLE_MISSING = "analyzable_article_title_missing"
    TITLE_TOO_SHORT = "analyzable_article_title_too_short"
    TITLE_TOO_LONG = "analyzable_article_title_too_long"
    BODY_MISSING = "analyzable_article_body_missing"
    BODY_TOO_SHORT = "analyzable_article_body_too_short"
    BODY_TOO_LONG = "analyzable_article_body_too_long"
    PUBLISHED_AT_MISSING = "analyzable_article_published_at_missing"
    SOURCE_ID_INVALID = "analyzable_article_source_id_invalid"
    # value は語順が逆 = 規約外。contract test では個別 assert する。
    UNMAPPED_VALIDATION_ERROR = "analyzable_article_validation_error_unmapped"


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
) -> tuple[tuple[AnalyzableArticleDefect, ...], tuple[str, ...]]:
    """``ValidationError`` を defect 集合 + 写像漏れ痕跡に分類する。

    再チェックでなく分類: Field constraint が捕えた error を ``(loc[0], type)`` で
    写像する。未知の組は ``UNMAPPED_VALIDATION_ERROR`` に落とし、生 ``"loc:type"``
    を ``unmapped`` に残し ``logger.warning`` で可視化する。
    """
    defects: list[AnalyzableArticleDefect] = []
    unmapped: list[str] = []
    for err in exc.errors():
        loc = err["loc"]
        field = str(loc[0]) if loc else ""
        error_type = err["type"]
        defect = _DEFECT_BY_LOC_TYPE.get((field, error_type))
        if defect is None:
            defects.append(AnalyzableArticleDefect.UNMAPPED_VALIDATION_ERROR)
            unmapped.append(f"{field}:{error_type}")
            logger.warning(
                "analyzable_article_validation_error_unmapped",
                loc=field,
                type=error_type,
            )
            continue
        defects.append(defect)
    return tuple(defects), tuple(unmapped)


@dataclass(frozen=True, slots=True)
class QualityTooLow:
    """不変条件に届かず構築できない理由を分類済み defect 集合で表す。

    free-text の Pydantic message (body 断片を含みうる = PII リスク) は保持せず、
    監査に焼くのは構造化 defect のみ。completer がこれを ``CompletionRejection``
    に翻訳する。
    """

    defects: tuple[AnalyzableArticleDefect, ...]
    unmapped: tuple[str, ...] = ()


class AnalyzableArticle(BaseModel):
    """分析工程に進める記事。

    不変条件 (Field で保証):
    - ``title``: 1..500 文字
    - ``body``: 50..1_048_576 文字
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
    source_url: CanonicalArticleUrl

    @classmethod
    def try_build(
        cls,
        *,
        title: str,
        body: str | None,
        published_at: PublishedAt | None,
        source_id: int,
        source_url: CanonicalArticleUrl,
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
        source_url: CanonicalArticleUrl,
    ) -> Self | QualityTooLow:
        """揃った材料から構築を試み、品質基準に届かなければ理由を値で返す。

        route 2 (完成段) 用の smart constructor。``try_build`` (route 1,
        ``Self | None``) と異なり、構築不能の理由を ``QualityTooLow`` の分類済み
        defect 集合として返す。

        ``title`` / ``body`` / ``published_at`` は ``... | None`` で受け、
        Pydantic の validation path で捕えた ``ValidationError`` を defect に
        分類する。published_at 欠落も title/body 長や source_id≤0 と同種の
        不変条件違反として単一経路で扱う。
        """
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
            defects, unmapped = _classify_defects(exc)
            return QualityTooLow(defects=defects, unmapped=unmapped)
