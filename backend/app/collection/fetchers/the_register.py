"""The Register 用 Fetcher — Pattern R+H (Pattern H 設計で実装、Atom feed)。

The Register の Atom フィードは ``<summary>`` に短いリード文しか出さず、
本文は HTML を別途取得して trafilatura で抽出する必要がある (Pattern R+H 分類)。

per-source 設計 (実 Atom 観察ベース):

- feed 形式は **Atom (RFC4287)**、``xml:lang="en"``
- ``<link rel="alternate" href>`` は **redirector URL**
  (``https://go.theregister.com/feed/<host>/<path>``)、
  ``_normalize_register_link`` で実 URL に展開してから ``SafeUrl`` 構築する
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.article.domain.value_objects import PublishedAt
from app.collection.fetchers.tools.rss_parser import RssEntry, RssParser
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl

_REDIRECTOR_PREFIX = "https://go.theregister.com/feed/"


def _normalize_register_link(raw: str) -> str:
    """``go.theregister.com/feed/<host>/<path>`` → ``https://<host>/<path>`` に直す。

    The Register の Atom フィードは ``<link href>`` がリダイレクタ経由
    (``https://go.theregister.com/feed/www.theregister.com/2026/...``) のため、
    プレフィックスを切り捨てて実 URL を再構築する。
    """
    if raw.startswith(_REDIRECTOR_PREFIX):
        return "https://" + raw[len(_REDIRECTOR_PREFIX) :]
    return raw


class TheRegisterFetcher:
    """The Register 用 Pattern H Fetcher (Pattern R+H = HTML 必須、Atom feed)。"""

    NAME: ClassVar[str] = "The Register"
    ENDPOINT_URL: ClassVar[str] = "https://www.theregister.com/headlines.atom"

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def fetch(self, source_id: int) -> AsyncIterator[IncompleteArticle]:
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
    ) -> IncompleteArticle | None:
        title = entry.title[:500]
        if not title:
            return None

        if not entry.link:
            return None
        normalized_link = _normalize_register_link(entry.link)
        try:
            source_url = CanonicalArticleUrl(normalized_link)
        except ValueError:
            return None

        published_at_hint = (
            PublishedAt(value=entry.published) if entry.published else None
        )

        return IncompleteArticle(
            title=title,
            source_id=source_id,
            source_url=source_url,
            published_at_hint=published_at_hint,
        )
