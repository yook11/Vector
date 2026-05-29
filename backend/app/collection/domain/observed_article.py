"""``ObservedArticle`` — 外部ソースから取得できた記事事実の値オブジェクト。

取れた事実だけを持つ (要否 / 優先は ``ArticleCompletionPolicy`` が決める)。
``incomplete_articles.staged_attributes`` (JSONB) に焼かれ、cron poller で
再 hydrate される (``model_dump(mode="json", by_alias=True)`` で永続化、
``model_validate`` で復元)。

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

    # identity: JSONB 非永続 (列が authoritative)。in-memory では必須。
    # 表層列 ``incomplete_articles.source_name`` / ``url`` が identity の
    # SSoT (spec ``Pending source identity refactor.md`` #1 倒立解消)。
    # ``alias=`` ではなく validation/serialization を分離するのは、Pylance が
    # ``populate_by_name=True`` を尊重せず ``alias`` 側でしか __init__ 引数を
    # 受け付けないと誤判定するため。分離すれば __init__ シグネチャは Python
    # 名となり、runtime も ``populate_by_name=True`` で両名受け付ける。
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
        """素材 + origin から ObservedArticle を確定構築する (Stage 1 converter 便宜)。

        Stage 1 converter 経由では per-source observation = 単一 origin で全 field を
        stamp する。本 factory は ``ObservedField`` lift を 3 回 / ``origin=origin``
        を 3 回 converter に散らさないための置き場で、VO 不変条件ではない
        (Stage 2 HTML 補完では per-field origin 混在が起きる)。

        title 不在は precondition で raise 済みのため required。``body`` の truthy
        判定 (空文字を None 扱い) は converter 元コードの semantics 維持。
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

    @classmethod
    def from_staged_attributes(
        cls,
        staged_attributes: Mapping[str, Any],
        *,
        source_name: SourceName,
        source_url: CanonicalArticleUrl,
    ) -> Self:
        """JSONB へ退避した観測値に authoritative identity を戻して復元する。"""
        try:
            raw = dict(staged_attributes)
        except (TypeError, ValueError) as exc:
            raise ObservedArticleInvalidError(
                reason=ObservedArticleInvalidReason.STAGED_ATTRIBUTES_NOT_OBJECT
            ) from exc
        raw["sourceName"] = str(source_name)
        raw["source_url"] = source_url
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            raise ObservedArticleInvalidError(
                reason=_classify_observed_article_error(exc)
            ) from exc

    def to_audit_fields(self) -> dict[str, bool | int | str | None]:
        """structured log / audit 向けの per-field 充足スナップショット。

        Stage 1 棄却 log (``article_conversion_rejected``) と key を揃え、
        Observed 成立 / 変換失敗を同じ key 集合で集計可能にする。

        値そのもの (title 文字列 / body 本文 / published_at 日時) は出さない:
        body は MB スケールになりうる外部入力でログ汚染 / PII / ストレージコスト
        リスクが大きく、Stage 1 監視に必要なのは「何が取れて何が欠けたか」だけ。
        per-field origin は ``ObservedField`` 設計どおり field 単位で出す
        (Stage 2 HTML 補完で origin が混在する将来に備える)。
        """
        return {
            "has_title": self.title is not None,
            "title_origin": (
                str(self.title.origin) if self.title is not None else None
            ),
            "has_body": self.body is not None,
            "body_origin": (str(self.body.origin) if self.body is not None else None),
            "body_length": (len(self.body.value) if self.body is not None else None),
            "has_published_at": self.published_at is not None,
            "published_at_origin": (
                str(self.published_at.origin) if self.published_at is not None else None
            ),
        }
