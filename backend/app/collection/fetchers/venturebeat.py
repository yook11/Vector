"""VentureBeat 用 Source。

per-source 設計: VB の RSS feed は ``<description>`` / ``<content:encoded>``
に full body (~12000 chars) を含み、HTML 取得を経由せずに本文を構築できる
(`spec collection-source-rss-research.md`)。VB が時折 Vercel Challenge で
5xx を返す問題を構造的に回避する目的で、body 候補を共通 builder に渡し
Ready 昇格を試みる。RSS 仕様変動で body が短くなった entry は builder の
fallback で ``ObservedArticle`` に流し、recovery 性を維持する。

HTTP 取得 / feedparser / SSRF guard / title plain text 正規化は L2
``RssParser`` (``tools.rss``) に集約済。本ファイルは L3 翻訳
(RssEntry → FetchedArticle) の per-source 責務だけを持つ。body picker は
WordPress VIP の truncate 差を吸収するため ``<content:encoded>`` と
``<description>`` の長い方を採用する。
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    SourceCompletionProfile,
)
from app.collection.fetchers.tools.fetch_tools import FetchTools
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssEntry
from app.shared.value_objects.source_name import SourceName

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


class VentureBeatSource:
    """VentureBeat 用 ``XxxSource`` (Pattern R、body 信用)。

    Ready / Incomplete の分岐は ``passport_builder`` 側に委ね、Source は body
    候補を組み立てることだけに専念する (``_pick_body`` / ``_strip_html``)。
    """

    name: ClassVar[SourceName] = SourceName("VentureBeat")
    endpoint_url: ClassVar[str] = "https://venturebeat.com/feed"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE

    @classmethod
    async def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        entries = await tools.rss.fetch(
            endpoint_url=cls.endpoint_url,
            source_name=str(cls.name),
            parse_mode="text",
        )
        for entry in entries:
            yield FetchedArticle(
                title=entry.title,
                url=entry.link,
                body=_strip_html(_pick_body(entry)) or None,
                published_at=entry.published,
            )
