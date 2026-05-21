"""``ObservedArticle`` — 外部ソースから取得できた記事事実の値オブジェクト。

取れた事実だけを持つ (要否 / 優先は ``SourceCompletionProfile`` が決める)。
``pending_html_articles.staged_attributes`` (JSONB) に焼かれ、cron poller で
再 hydrate される (``model_dump(mode="json", by_alias=True)`` で永続化、
``model_validate`` で復元)。

- identity ``source_name`` / ``source_url`` は表層列が authoritative
  (``pending_html_articles.source_name`` NOT NULL + composite FK /
  ``pending_html_articles.url`` UNIQUE)。JSONB には焼かない
  (``Field(exclude=True)``) — in-memory では運搬のため必須。Stage 2 reader
  (``ArticleCompletionRepository``) は表層列の値を ``model_validate`` 前に
  raw へ注入する。
- ``origin`` は audit メタで merge を駆動しない。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.value_objects import PublishedAt
from app.shared.value_objects.source_name import SourceName


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
    # 表層列 ``pending_html_articles.source_name`` / ``url`` が identity の
    # SSoT (spec ``Pending source identity refactor.md`` #1 倒立解消)。
    source_name: SourceName = Field(alias="sourceName", exclude=True)
    source_url: CanonicalArticleUrl = Field(exclude=True)
    title: ObservedField[str] | None = None
    body: ObservedField[str] | None = None
    published_at: ObservedField[PublishedAt] | None = Field(
        default=None, alias="publishedAt"
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

    def to_audit_fields(self) -> dict[str, bool | int | str | None]:
        """structured log / audit 向けの per-field 充足スナップショット。

        Stage 1 失敗 log (``fetched_article_conversion_failed``) と key を揃え、
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
