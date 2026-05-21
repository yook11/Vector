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
