"""The Register 用 Source (Atom feed)。

Atom フィードは ``<summary>`` に短いリード文しか出さないため body は HTML
抽出に委ねる。``<link rel="alternate" href>`` は redirector URL
(``https://go.theregister.com/feed/<host>/<path>``) のため
``_normalize_register_link`` で実 URL に展開してから渡す。
"""

from __future__ import annotations

from typing import ClassVar

from app.collection.article_collection.fetched_article import FetchedArticle
from app.collection.article_collection.reader.rss_reader import RssEntry
from app.collection.article_collection.tools.reader_tools import ReaderTools
from app.collection.domain.observed_article import ObservedOrigin
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.collection.sources.base_article_source import BaseArticleSource
from app.shared.value_objects.source_name import SourceName

_REDIRECTOR_PREFIX = "https://go.theregister.com/feed/"


def _normalize_register_link(raw: str) -> str:
    """``go.theregister.com/feed/<host>/<path>`` → ``https://<host>/<path>`` に直す。"""
    if raw.startswith(_REDIRECTOR_PREFIX):
        return "https://" + raw[len(_REDIRECTOR_PREFIX) :]
    return raw


class TheRegisterSource(BaseArticleSource):
    """The Register 用 Source (Atom feed)。"""

    name: ClassVar[SourceName] = SourceName("The Register")
    endpoint_url: ClassVar[str] = "https://www.theregister.com/headlines.atom"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[RssEntry]:
        return await tools.rss.fetch(
            endpoint_url=cls.endpoint_url,
            source_name=str(cls.name),
            parse_mode="text",
        )

    @classmethod
    def map_entry(cls, entry: RssEntry) -> FetchedArticle:
        """RSS body を信用しないため body は採らない。"""
        return FetchedArticle(
            title=entry.title,
            url=_normalize_register_link(entry.link),
            body=None,
            published_at=entry.published,
        )
