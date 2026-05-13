"""ingestion BC 出口型 — Fetcher が yield する型を定義する。

各 Fetcher は ``AsyncIterator[FetchOutcome]`` を返す。``ReadyForArticle`` が
次工程進行保証型 (passport) で、何が取れようがこれを満たして次工程に渡す。
``metadata`` は ``FetchedEntry`` で Service まで運ばれ、Stage 1 で
``pipeline_events.payload`` に焼き付けた後は以降の段階に運ばない。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.collection.extraction.domain.value_objects import PublishedAt
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl

_TITLE_MIN_LENGTH = 1
_TITLE_MAX_LENGTH = 500
_BODY_MIN_LENGTH = 50
_BODY_MAX_LENGTH = 1_048_576  # 1 MiB

FailureCode = Literal[
    "http_transient",
    "http_blocked",
    "paywalled",
    "extraction_empty",
    "body_too_short",
    "title_missing",
    "published_at_missing",
    "link_target_failed",
    "other",
]


class FailureReason(BaseModel):
    """``retryable`` は scheduler 再投入判定。``detail`` は同 ``code`` の細分化。"""

    model_config = ConfigDict(frozen=True)

    code: FailureCode
    retryable: bool
    detail: str | None = None


class PendingHtmlFetch(BaseModel):
    """Pattern H 1 段目の中間 passport (Stage 2 で ``ReadyForArticle`` に昇格)。

    ``pending_html_articles.staged_attributes`` (JSONB) に焼かれて永続化され、
    Stage 2 cron poller (``dispatch_html_fetch_jobs``) で再 hydrate される。
    BaseModel(frozen=True) は taskiq 経由ではなく不変表明のため
    (memory `feedback_taskiq_basemodel_required.md`)。``prefer_html_title`` は
    sitemap 系ソース (RSS が title を持たない) のための opt-in flag。
    """

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=_TITLE_MIN_LENGTH, max_length=_TITLE_MAX_LENGTH)
    source_id: int = Field(gt=0)
    source_url: CanonicalArticleUrl
    published_at_hint: PublishedAt | None = None
    prefer_html_title: bool = False


class ReadyForArticle(BaseModel):
    """次工程進行保証型 (passport)。Pattern R Fetcher 直接 / Pattern H Stage 2 で構築。

    各 Fetcher は何が取れようがこれを満たして次工程に渡す。補足情報は
    ``FetchedEntry.metadata`` で別軸に運び、Stage 1 で ``pipeline_events`` に焼く。
    """

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=_TITLE_MIN_LENGTH, max_length=_TITLE_MAX_LENGTH)
    body: str = Field(min_length=_BODY_MIN_LENGTH, max_length=_BODY_MAX_LENGTH)
    published_at: PublishedAt
    source_id: int = Field(gt=0)
    source_url: CanonicalArticleUrl

    @classmethod
    def try_advance_from(
        cls,
        pending: PendingHtmlFetch,
        body: str,
        html_published_at: PublishedAt | None,
        html_title: str | None = None,
    ) -> ReadyForArticle | Failed:
        """Merge 規則: title は RSS 優先 (``prefer_html_title`` で HTML 採用に切替)、
        published_at は RSS hint 優先 / HTML フォールバック、両方欠落で Failed。
        """
        final_published = pending.published_at_hint or html_published_at
        if final_published is None:
            return Failed(
                reason=FailureReason(
                    code="published_at_missing",
                    retryable=False,
                    detail="rss_and_html_both_missing",
                )
            )
        final_title = (
            html_title if (pending.prefer_html_title and html_title) else pending.title
        )
        try:
            return cls(
                title=final_title,
                body=body,
                published_at=final_published,
                source_id=pending.source_id,
                source_url=pending.source_url,
            )
        except ValueError as e:
            return Failed(
                reason=FailureReason(
                    code="other",
                    retryable=False,
                    detail=f"invariant_violation:{e}",
                )
            )


class Failed(BaseModel):
    model_config = ConfigDict(frozen=True)

    reason: FailureReason


@dataclass(frozen=True, slots=True)
class FetchedEntry:
    """Fetcher が 1 entry 分 yield する単位 (Service 内消費のみ、kiq に乗らない)。

    ``metadata`` は opaque な dict。Fetcher 側で primitive (str/int/list/dict)
    に正規化してから格納する責務 (URL は ``str(url)`` 化、datetime は
    ``isoformat()`` 化)。``pipeline_events.payload`` (JSONB) に直接焼かれる。
    """

    item: ReadyForArticle | PendingHtmlFetch
    metadata: Mapping[str, Any]


FetchOutcome = FetchedEntry | Failed
