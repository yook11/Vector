"""``ObservedArticle`` — 外部ソースから取得できた記事事実の値オブジェクト。

取れた事実だけを持つ (要否 / 優先は ``ArticleCompletionPolicy`` が決める)。
``incomplete_articles.staged_attributes`` (JSONB) に焼かれ、cron poller で
再 hydrate される (``to_staged_attributes`` で永続化、
``from_staged_attributes`` で復元)。

- identity ``source_name`` / ``source_url`` は表層列が authoritative
  (``incomplete_articles.source_name`` NOT NULL + composite FK /
  ``incomplete_articles.url`` UNIQUE)。JSONB には焼かない
  (``Field(exclude=True)``) — in-memory では運搬のため必須。Stage 2 reader
  (``ArticleCompletionRepository``) は表層列の値を ``model_validate`` 前に
  raw へ注入する。
- ``origin`` は audit メタで merge を駆動しない。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.value_objects import PublishedAt
from app.collection.sources.source_name import SourceName


class ObservedArticleInvalidReason(StrEnum):
    """ObservedArticle 復元失敗の field 単位分類 (PII-free、値だけで読める)。"""

    STAGED_ATTRIBUTES_NOT_OBJECT = "staged_attributes_not_object"
    SOURCE_NAME_MISSING = "source_name_missing"
    SOURCE_NAME_INVALID = "source_name_invalid"
    SOURCE_URL_INVALID = "source_url_invalid"
    TITLE_INVALID = "title_invalid"
    BODY_INVALID = "body_invalid"
    PUBLISHED_AT_INVALID = "published_at_invalid"
    OBSERVED_FIELD_INVALID = "observed_field_invalid"


class ObservedArticleInvalidError(Exception):
    """ObservedArticle として復元できない入力。reason で失敗 field を分類する。"""

    MESSAGE: ClassVar[str] = "observed article input is invalid"

    def __init__(self, *, reason: ObservedArticleInvalidReason) -> None:
        self.reason = reason
        super().__init__(f"{self.MESSAGE}: {reason}")


def _classify_observed_article_error(
    exc: ValidationError,
) -> ObservedArticleInvalidReason:
    """loc[0]/type で失敗 field を分類する (input 非参照で PII フリー)。

    identity (sourceName/source_url) を content より優先: sourceName 欠落は
    ACL/repository 注入漏れ、source_url 不正は DB URL 汚染 / canonical VO 由来で、
    content 系と「次に見る場所」が違う。alias 形と populate_by_name 名の両方を拾う。
    """
    fields = {str(e["loc"][0]): e for e in exc.errors() if e.get("loc")}
    source_name_err = fields.get("sourceName") or fields.get("source_name")
    if source_name_err is not None:
        if source_name_err.get("type") == "missing":
            return ObservedArticleInvalidReason.SOURCE_NAME_MISSING
        return ObservedArticleInvalidReason.SOURCE_NAME_INVALID
    if "source_url" in fields:
        return ObservedArticleInvalidReason.SOURCE_URL_INVALID
    if "title" in fields:
        return ObservedArticleInvalidReason.TITLE_INVALID
    if "body" in fields:
        return ObservedArticleInvalidReason.BODY_INVALID
    if "publishedAt" in fields or "published_at" in fields:
        return ObservedArticleInvalidReason.PUBLISHED_AT_INVALID
    return ObservedArticleInvalidReason.OBSERVED_FIELD_INVALID


class ObservedOrigin(StrEnum):
    """観測値の出自 (audit only — merge を駆動しない)。"""

    feed = "feed"  # RSS / Atom item
    sitemap = "sitemap"  # sitemap <loc>/<lastmod>
    listing = "listing"  # HTML listing / landing page
    api = "api"  # JSON API


class ObservedField[T](BaseModel):
    """1 フィールドの観測事実: 値 + 出自。取れなかった場合は親側で ``None``。"""

    model_config = ConfigDict(frozen=True)

    value: T
    origin: ObservedOrigin


class ObservedArticle(BaseModel):
    """外部取得できた記事事実の単一値型。"""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    # source_name / source_url は表層列が authoritative なため JSONB 非永続
    # (exclude=True)。in-memory では運搬に必須。
    # alias= でなく validation/serialization を分離するのは、Pylance が
    # populate_by_name=True を無視し alias 側しか __init__ 引数に認めない
    # 誤検出を避けるため (runtime は両名受け付ける)。
    source_name: SourceName = Field(
        validation_alias="sourceName",
        serialization_alias="sourceName",
        exclude=True,
    )
    source_url: CanonicalArticleUrl = Field(exclude=True)
    title: ObservedField[str] | None = None
    body: ObservedField[str] | None = None
    published_at: ObservedField[PublishedAt] | None = Field(
        default=None,
        validation_alias="publishedAt",
        serialization_alias="publishedAt",
    )

    @classmethod
    def build(
        cls,
        *,
        source_name: SourceName,
        source_url: CanonicalArticleUrl,
        title: str,
        body: str | None,
        published_at: PublishedAt | None,
        origin: ObservedOrigin,
    ) -> Self:
        """素材 + origin から ObservedArticle を構築する (Stage 1 converter 用)。

        単一 origin で全 field を stamp する便宜 factory で、VO 不変条件ではない
        (Stage 2 補完では per-field origin が混在する)。title は precondition で
        非空が保証済みのため required。
        """
        return cls(
            source_name=source_name,
            source_url=source_url,
            title=ObservedField(value=title, origin=origin),
            body=ObservedField(value=body, origin=origin) if body else None,
            published_at=(
                ObservedField(value=published_at, origin=origin)
                if published_at is not None
                else None
            ),
        )

    def to_staged_attributes(self) -> dict[str, Any]:
        """``staged_attributes`` (JSONB) へ焼く永続化形式に変換する。

        identity (``source_name`` / ``source_url``) は ``exclude=True`` のため
        含まれない (表層列が authoritative)。``from_staged_attributes`` の逆写像。
        """
        return self.model_dump(mode="json", by_alias=True)

    @classmethod
    def from_staged_attributes(
        cls,
        staged_attributes: Mapping[str, Any],
        *,
        source_name: SourceName,
        source_url: CanonicalArticleUrl,
    ) -> Self:
        """JSONB 永続化形式をほどき、検証済み identity を差し戻して復元する。"""
        if not isinstance(staged_attributes, Mapping):
            raise ObservedArticleInvalidError(
                reason=ObservedArticleInvalidReason.STAGED_ATTRIBUTES_NOT_OBJECT
            )
        # identity は JSONB 非永続なので検証済み VO を差し戻す。
        observed_input = {
            **staged_attributes,
            "source_name": str(source_name),
            "source_url": source_url,
        }
        try:
            return cls.model_validate(observed_input)
        except ValidationError as exc:
            raise ObservedArticleInvalidError(
                reason=_classify_observed_article_error(exc)
            ) from exc
