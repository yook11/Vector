"""The Register 用 Source (Atom feed)。

Atom フィードは ``<summary>`` に短いリード文しか出さないため body は HTML
抽出に委ねる。``<link rel="alternate" href>`` は redirector URL
(``https://go.theregister.com/feed/<host>/<path>``) のため
``_normalize_register_link`` で実 URL に展開してから渡す。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    SourceCompletionProfile,
)
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.reader.rss_reader import RssEntry
from app.collection.source_fetch.tools.fetch_tools import FetchTools
from app.shared.value_objects.source_name import SourceName

_REDIRECTOR_PREFIX = "https://go.theregister.com/feed/"


def _normalize_register_link(raw: str) -> str:
    """``go.theregister.com/feed/<host>/<path>`` → ``https://<host>/<path>`` に直す。"""
    if raw.startswith(_REDIRECTOR_PREFIX):
        return "https://" + raw[len(_REDIRECTOR_PREFIX) :]
    return raw


class TheRegisterSource:
    """The Register 用 Source (Atom feed)。"""

    name: ClassVar[SourceName] = SourceName("The Register")
    endpoint_url: ClassVar[str] = "https://www.theregister.com/headlines.atom"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_profile: ClassVar[SourceCompletionProfile] = DEFAULT_PROFILE

    @classmethod
    def to_fetched_article(cls, entry: RssEntry) -> FetchedArticle:
        """RSS body を信用しないため body は採らない。"""
        return FetchedArticle(
            title=entry.title,
            url=_normalize_register_link(entry.link),
            body=None,
            published_at=entry.published,
        )

    @classmethod
    async def collect(cls, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        entries = await tools.rss.fetch(
            endpoint_url=cls.endpoint_url,
            source_name=str(cls.name),
            parse_mode="text",
        )
        for entry in entries:
            yield cls.to_fetched_article(entry)
