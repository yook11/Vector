"""抽出候補 VO — extraction 境界の中間表現と Repo ルックアップ結果型。

HTML 抽出の生結果 (:class:`HtmlExtractionResult`) を Article 永続化に耐える
形に正規化した :class:`ArticleExtractedContent` と、
DiscoveredArticle の抽出可否を型で表現する sum type を提供する。

- :class:`PublishedAt` — tzinfo=UTC を invariant として保持する公開日時 VO。
- :class:`ArticleExtractedContent` — title/body 必須・published_at 任意の永続化候補。
- :class:`UnextractedDiscoveredArticle` — Article 未生成の DiscoveredArticle を表現。
- :data:`DiscoveredArticleLookup` — Repo が返すルックアップ結果 sum type。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self

from app.collection.extraction.extractor import HtmlExtractionResult
from app.domain.safe_url import SafeUrl

_TITLE_MAX_LENGTH = 500
_BODY_MIN_LENGTH = 50


@dataclass(frozen=True)
class PublishedAt:
    """公開日時 VO — tzinfo=UTC を構造的に保証する。

    trafilatura は htmldate 経由で TZ なしの文字列を返すため、
    ここで UTC として解釈して型に閉じ込める。
    """

    value: datetime

    def __post_init__(self) -> None:
        if self.value.tzinfo is None:
            raise ValueError("PublishedAt.value must be timezone-aware")

    @classmethod
    def parse(cls, raw: str | None) -> Self | None:
        """trafilatura の日付文字列を解釈する。解釈不能なら ``None``。"""
        if not raw:
            return None
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return cls(datetime.strptime(raw, fmt).replace(tzinfo=UTC))
            except ValueError:
                continue
        return None


@dataclass(frozen=True)
class ArticleExtractedContent:
    """Article 永続化候補 — 品質ゲートを通過した抽出結果。

    invariant:
      - ``title``: 非空、500 文字以内
      - ``body``: 50 文字以上
      - ``published_at``: 任意
    """

    title: str
    body: str
    published_at: PublishedAt | None

    def __post_init__(self) -> None:
        if not self.title:
            raise ValueError("title must be non-empty")
        if len(self.title) > _TITLE_MAX_LENGTH:
            raise ValueError(f"title exceeds {_TITLE_MAX_LENGTH} chars")
        if len(self.body) < _BODY_MIN_LENGTH:
            raise ValueError(f"body must be at least {_BODY_MIN_LENGTH} chars")

    @classmethod
    def from_extraction(cls, result: HtmlExtractionResult) -> Self | None:
        """生抽出結果から候補を構築する。品質ゲート未達なら ``None``。"""
        if result.title is None or result.body is None:
            return None
        published_at = (
            PublishedAt(result.published_at)
            if result.published_at is not None
            else None
        )
        return cls(title=result.title, body=result.body, published_at=published_at)


@dataclass(frozen=True)
class UnextractedDiscoveredArticle:
    """Article が未生成の DiscoveredArticle。Repo が構築時点で保証する。"""

    id: int
    url: SafeUrl


@dataclass(frozen=True)
class UnextractedFound:
    """ルックアップ結果: 抽出対象の DiscoveredArticle が存在する。"""

    article: UnextractedDiscoveredArticle


@dataclass(frozen=True)
class AlreadyExtracted:
    """ルックアップ結果: 既に Article が存在する（冪等ヒット）。"""

    article_id: int


@dataclass(frozen=True)
class DiscoveredNotFound:
    """ルックアップ結果: DiscoveredArticle が存在しない。"""


DiscoveredArticleLookup = UnextractedFound | AlreadyExtracted | DiscoveredNotFound
