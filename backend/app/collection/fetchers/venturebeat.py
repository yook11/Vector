"""VentureBeat 用 Fetcher。

per-source 設計: VB の RSS feed は ``<description>`` / ``<content:encoded>``
に full body (~12000 chars) を含み、HTML 取得を経由せずに本文を構築できる
(`spec collection-source-rss-research.md`)。VB が時折 Vercel Challenge で
5xx を返す問題を構造的に回避する目的で、body 候補を共通 builder に渡し
Ready 昇格を試みる。RSS 仕様変動で body が短くなった entry は builder の
fallback で ``IncompleteArticle`` に流し、recovery 性を維持する。

HTTP 取得 / feedparser / SSRF guard / title plain text 正規化は L2
``RssParser`` に集約済。本ファイルは L3 翻訳 (RssEntry → passport) の
per-source 責務だけを持つ。body picker は WordPress VIP の truncate 差を
吸収するため ``<content:encoded>`` と ``<description>`` の長い方を採用する。
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.article.domain.article import ReadyForArticle
from app.collection.fetchers.tools.passport_builder import try_build_passport
from app.collection.fetchers.tools.rss_parser import RssEntry, RssParser
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    """HTML tag 除去 + entity decode + 空白圧縮。body の plain text 化に使う。"""
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", s))).strip()


def _pick_body(entry: RssEntry) -> str:
    """``content_encoded`` と ``summary`` の長い方を本文として採用する。

    一部 WordPress VIP サイト (VB / IEEE Spectrum 等) では片方が truncate
    される運用上の差分があり、長い方を採用するロジックで吸収する
    (`spec collection-source-rss-research.md` の「max(content_encoded, summary)」)。
    """
    content_encoded = entry.content_encoded or ""
    summary = entry.summary or ""
    return content_encoded if len(content_encoded) >= len(summary) else summary


class VentureBeatFetcher:
    """VentureBeat 用 Fetcher。"""

    NAME: ClassVar[str] = "VentureBeat"
    ENDPOINT_URL: ClassVar[str] = "https://venturebeat.com/feed"

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def fetch(
        self, source_id: int
    ) -> AsyncIterator[ReadyForArticle | IncompleteArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self.ENDPOINT_URL,
            source_name=self.NAME,
            parse_mode="text",
        )
        for entry in entries:
            item = self._convert_entry(entry, source_id)
            if item is not None:
                yield item

    def _convert_entry(
        self,
        entry: RssEntry,
        source_id: int,
    ) -> ReadyForArticle | IncompleteArticle | None:
        """1 entry を passport に変換する。VB は RSS 本文を信用するため
        body 候補を builder に渡し Ready 昇格を試みる。"""
        return try_build_passport(
            title=entry.title,
            link=entry.link,
            body_candidate=_strip_html(_pick_body(entry)) or None,
            published_hint=entry.published,
            source_id=source_id,
        )
