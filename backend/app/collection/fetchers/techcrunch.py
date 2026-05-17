"""TechCrunch 用 Source。

per-source 設計: TC の RSS feed は ``<description>`` にリード文 (~140 chars)
しか含まず ``<content:encoded>`` も提供しない (`spec
collection-source-rss-research.md`)。RSS 本文を信用しない —
``body=None`` を yield し、URL + title を ``ObservedArticle`` として後段に
渡す。後段の ``ArticleCompletionService`` が HTML 本文を取得 + promotion
する 2 段構成。

将来 TC が ``<content:encoded>`` に full body を載せるようになった場合、
``collect`` で body 候補を組み立てれば自然に Ready 経路に切り替わる。

HTTP 取得 / feedparser / SSRF guard / title plain text 正規化は L2
``RssParser`` (``tools.rss``) に集約済。本ファイルは L3 翻訳
(RssEntry → FetchedArticle) の per-source 責務だけを持つ。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    SourceCompletionProfile,
)
from app.collection.fetchers.tools.fetch_tools import FetchTools
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.shared.value_objects.source_name import SourceName


class TechCrunchSource:
    """TechCrunch 用 ``XxxSource`` (Pattern H、body 不信用)。"""

    name: ClassVar[SourceName] = SourceName("TechCrunch")
    endpoint_url: ClassVar[str] = "https://techcrunch.com/feed/"
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
                body=None,
                published_at=entry.published,
            )
