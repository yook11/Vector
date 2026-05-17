"""``ObservedArticle`` — 外部ソースから取得できた記事事実の値オブジェクト。

補完待ち獲得経路で取得できた記事事実を運ぶ単一型。
「補完待ち」という lifecycle 状態は ``pending_html_articles`` 行が表現済みで、
domain mirror を別に作ると二重表現になるため作らない (spec §1.3/§4.4)。本型は
**取れた事実だけ**を持ち、要否/優先は ``SourceCompletionProfile`` が決める。

Pattern H で ``passport_builder`` が yield する中間表現であり、
``pending_html_articles.staged_attributes`` (JSONB) に焼かれて Stage 2 cron
poller で再 hydrate される。``model_dump(mode="json", by_alias=True)`` で永続化、
``model_validate`` で復元する。

責務分離 (spec §4.5/§7):

- ``source_url`` は記事 identity (``pending_html_articles.url`` UNIQUE 列が
  唯一の authoritative)。JSONB には焼かない — ``Field(exclude=True)`` で
  シリアライズを型レベルで常時除外し、二重管理 (drift) を構造的に排除する。
  Stage 1 の passport→enqueue 運搬のため in-memory では必須で保持する。
- ``origin`` は audit メタで merge を駆動しない (``Source.observed_origin``
  由来。RSS=feed / Anthropic=sitemap / ORNL=listing / API=api)。
- 後方互換: ``schemaVersion`` 不在 = 旧形 (legacy) JSONB。
  before-validator が **observed shape のみ** 変換する (DB 非アクセス)。
  legacy 行の ``sourceName`` / ``source_url`` は repository (ACL) が列/resolver
  から ``model_validate`` 前に raw へ注入する。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.collection.domain.value_objects import PublishedAt
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl
from app.shared.value_objects.source_name import SourceName


class ObservedOrigin(StrEnum):
    """観測値の出自 (audit only — merge を駆動しない)。

    ``Source.observed_origin`` がソースごとに宣言し、``passport_builder`` が
    ``ObservedField.origin`` に stamp する。
    """

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
    """外部取得できた記事事実の単一値型 (Pattern H passport / JSONB 契約)。"""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    schema_version: Literal[1] = Field(default=1, alias="schemaVersion")
    source_name: SourceName = Field(alias="sourceName")
    # identity: JSONB 非永続 (列が authoritative)。in-memory では必須。
    source_url: CanonicalArticleUrl = Field(exclude=True)
    title: ObservedField[str] | None = None
    body: ObservedField[str] | None = None
    published_at: ObservedField[PublishedAt] | None = Field(
        default=None, alias="publishedAt"
    )

    @model_validator(mode="before")
    @classmethod
    def _absorb_legacy(cls, data: Any) -> Any:
        """旧形 (legacy) JSONB (``schemaVersion`` 不在) を
        新 observed 形へ変換する。**shape のみ** — DB は触らない。

        旧形 = ``{title, published_at_hint, prefer_html_title}``。
        ``title`` → ``title{value, origin:feed}`` / ``published_at_hint`` (非
        null) → ``published_at{value, origin:feed}`` / ``body`` → 不在 /
        ``prefer_html_title`` → 破棄 (policy は profile 所有)。``sourceName`` /
        ``source_url`` は repository が legacy raw へ事前注入するため、ここでは
        carry-through するだけで生成しない。

        legacy 判定は legacy 専用キー (``prefer_html_title`` /
        ``published_at_hint``) の存在で行う。``schemaVersion`` 不在のみで
        判定すると、新規 in-memory 構築 (kwargs に schemaVersion を含めない =
        default) を legacy と誤認するため不可。
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
