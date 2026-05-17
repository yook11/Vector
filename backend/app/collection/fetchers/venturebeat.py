"""VentureBeat 用 Fetcher。

per-source 設計: VB の RSS feed は ``<description>`` / ``<content:encoded>``
に full body (~12000 chars) を含み、HTML 取得を経由せずに本文を構築できる
(`spec collection-source-rss-research.md`)。VB が時折 Vercel Challenge で
5xx を返す問題を構造的に回避する目的で、body 候補を共通 builder に渡し
Ready 昇格を試みる。RSS 仕様変動で body が短くなった entry は builder の
fallback で ``ObservedArticle`` に流し、recovery 性を維持する。

HTTP 取得 / feedparser / SSRF guard / title plain text 正規化は L2
``RssParser`` に集約済。本ファイルは L3 翻訳 (RssEntry → passport) の
per-source 責務だけを持つ。body picker は WordPress VIP の truncate 差を
吸収するため ``<content:encoded>`` と ``<description>`` の長い方を採用する。
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator

from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssEntry, RssParser

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


class VentureBeatAdapter:
    """VentureBeat 用 SourceAdapter (新経路、Adapter 駆動)。

    旧 ``VentureBeatFetcher`` と並存させ、移行期間中は両方が同じ module helper
    (``_pick_body`` / ``_strip_html``) を共有する。P6 で ``strategy.py`` を
    ``ArticleFetcher(VentureBeatAdapter())`` 形に切替後、P7 cleanup で旧 Fetcher
    を削除する予定。
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        source_name: str,
        parser: RssParser | None = None,
    ) -> None:
        self._endpoint_url = endpoint_url
        self._source_name = source_name
        self._parser = parser or RssParser()

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self._endpoint_url,
            source_name=self._source_name,
            parse_mode="text",
        )
        for entry in entries:
            yield self._to_fetched(entry)

    def _to_fetched(self, entry: RssEntry) -> FetchedArticle:
        """1 ``RssEntry`` を ``FetchedArticle`` に翻訳する。Ready / Incomplete
        の分岐は ``passport_builder`` 側に委ね、Adapter は body 候補を組み立てる
        ことだけに専念する。"""
        body_candidate = _strip_html(_pick_body(entry)) or None
        return FetchedArticle(
            title=entry.title,
            url=entry.link,
            body=body_candidate,
            published_at=entry.published,
        )
