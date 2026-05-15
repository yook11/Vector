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

from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssParser

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


class TheRegisterAdapter:
    """The Register 用 SourceAdapter (Pattern H、Atom feed)。

    ``<link href>`` は redirector 経由 (``go.theregister.com/feed/...``) のため
    ``_normalize_register_link`` で実 URL に展開してから渡す (builder では
    復元できない per-source 変換)。title / URL の構造ゲートは
    ``passport_builder`` に委譲する。
    """

    NAME = "The Register"
    ENDPOINT_URL = "https://www.theregister.com/headlines.atom"

    def __init__(self, parser: RssParser | None = None) -> None:
        self._parser = parser or RssParser()

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        entries = await self._parser.fetch(
            endpoint_url=self.ENDPOINT_URL,
            source_name=self.NAME,
            parse_mode="text",
        )
        for entry in entries:
            if not entry.link:
                continue
            yield FetchedArticle(
                title=entry.title,
                url=_normalize_register_link(entry.link),
                body=None,
                published_at=entry.published,
            )
