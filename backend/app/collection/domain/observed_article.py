"""``ObservedArticle`` — 外部ソースから取得できた記事事実の値オブジェクト。

取れた事実だけを持つ (要否 / 優先は ``SourceCompletionProfile`` が決める)。
``pending_html_articles.staged_attributes`` (JSONB) に焼かれ、cron poller で
再 hydrate される (``model_dump(mode="json", by_alias=True)`` で永続化、
``model_validate`` で復元)。

- identity ``source_name`` / ``source_url`` は表層列が authoritative
  (``pending_html_articles.source_name`` NOT NULL + composite FK /
  ``pending_html_articles.url`` UNIQUE)。JSONB には焼かない
  (``Field(exclude=True)``) — in-memory では運搬のため必須。Stage 2 reader
  (``ArticleCompletionRepository``) は新形 / legacy を区別せず、常に表層列から
  ``model_validate`` 前に raw へ注入する。
- ``origin`` は audit メタで merge を駆動しない。
- 後方互換: ``schemaVersion`` 不在 = 旧形 JSONB。before-validator が shape のみ
  変換する (DB 非アクセス、title→{value, origin} 等の構造化)。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.collection.domain.value_objects import PublishedAt
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl
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

    schema_version: Literal[1] = Field(default=1, alias="schemaVersion")
    # identity: JSONB 非永続 (列が authoritative)。in-memory では必須。
    # ``source_name`` も ``source_url`` と同形で表層列に identity を移譲する
    # (spec ``Pending source identity refactor.md`` #1 倒立解消)。
    # ``_absorb_legacy`` の ``sourceName`` carry-through は legacy 行
    # (旧 JSONB に焼かれた sourceName) の hydrate 経路として保つ。
    source_name: SourceName = Field(alias="sourceName", exclude=True)
    source_url: CanonicalArticleUrl = Field(exclude=True)
    title: ObservedField[str] | None = None
    body: ObservedField[str] | None = None
    published_at: ObservedField[PublishedAt] | None = Field(
        default=None, alias="publishedAt"
    )

    @model_validator(mode="before")
    @classmethod
    def _absorb_legacy(cls, data: Any) -> Any:
        """旧形 JSONB を新 observed 形へ変換する。shape のみ — DB は触らない。

        旧形 = ``{title, published_at_hint, prefer_html_title}``。``title`` /
        ``published_at_hint`` (非 null) を ``{value, origin:feed}`` に包む。
        ``prefer_html_title`` は破棄。``sourceName`` / ``source_url`` は
        carry-through のみ (repository が legacy raw へ事前注入する)。

        legacy 判定は legacy 専用キーの存在で行う。``schemaVersion`` 不在のみだと
        新規 in-memory 構築 (default) を legacy と誤認するため不可。
        """
        if not isinstance(data, dict):
            return data
        if "schemaVersion" in data or "schema_version" in data:
            return data
        if "prefer_html_title" not in data and "published_at_hint" not in data:
            return data  # 新形 (programmatic 構築 / observed 全欠)

        observed_title = data.get("title")
        hint = data.get("published_at_hint")
        return {
            "schemaVersion": 1,
            "sourceName": data.get("sourceName") or data.get("source_name"),
            "source_url": data.get("source_url"),
            "title": (
                {"value": observed_title, "origin": ObservedOrigin.feed}
                if observed_title is not None
                else None
            ),
            "body": None,
            "publishedAt": (
                {"value": hint, "origin": ObservedOrigin.feed}
                if hint is not None
                else None
            ),
        }
