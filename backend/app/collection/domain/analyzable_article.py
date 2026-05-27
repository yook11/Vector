"""``AnalyzableArticle`` — 分析工程に進める記事 (collection BC の出口契約)。

``id`` を持たない (identity は永続化後の関心事)。長さ境界の SSoT は
``article_limits``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from app.collection.domain.article_limits import (
    ARTICLE_BODY_MAX_LENGTH,
    ARTICLE_BODY_MIN_LENGTH,
    ARTICLE_TITLE_MAX_LENGTH,
    ARTICLE_TITLE_MIN_LENGTH,
)
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.value_objects import PublishedAt


@dataclass(frozen=True, slots=True)
class QualityTooLow:
    """揃った材料が品質基準 (title/body 長 / published_at 欠落等) に届かず
    構築できない理由。

    construct を拒んだ ``ValueError`` の証拠 (class 名 + message) を保持する。
    published_at 欠落も他の不変条件違反と同様にこの型へ畳む。
    completer がこれを audit 語彙 ``CompletionRejection`` に翻訳する。
    message の長さ上限は下流の翻訳先が担うため、本型は最小に保つ。
    """

    error_class: str
    error_message: str


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
        ``Self | None``) と異なり、構築不能の理由を ``QualityTooLow`` の証拠として
        返す。

        ``title`` / ``body`` / ``published_at`` は ``... | None`` で受け、
        Pydantic の validation path で ``QualityTooLow`` に畳む。published_at
        欠落も title/body 長や source_id≤0 と同種の不変条件違反として単一経路で
        扱う。
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
        except ValueError as e:
            return QualityTooLow(error_class=type(e).__name__, error_message=str(e))
