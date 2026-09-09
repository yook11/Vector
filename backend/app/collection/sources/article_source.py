"""未移行ソースの取得契約と、新旧ソースを受ける取得入口の型。"""

from __future__ import annotations

from typing import ClassVar, Protocol, TypeVar, runtime_checkable

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.sources.rss_acquisition import RssSource
from app.collection.sources.source_metadata import SourceMetadata

# その source が使う Reader の Entry 型 (RssEntry / SitemapEntry / ...)。
T = TypeVar("T")


@runtime_checkable
class ArticleSource(SourceMetadata, Protocol[T]):
    """未移行ソースが持つ取得・選別・写像の契約。"""

    endpoint_url: ClassVar[str]

    @classmethod
    async def read(cls, tools: ReaderTools) -> list[T]:
        """どの Reader を params 付きで呼ぶか (thin binding)。"""
        ...

    @classmethod
    def in_scope(cls, entry: T) -> bool:
        """収集スコープ述語 (default: 全件採用)。"""
        ...

    @classmethod
    def select(cls, entries: list[T]) -> list[T]:
        """採用する entry 列を最終決定する純粋処理 = dedup / order / limit。"""
        ...

    @classmethod
    def map_entry(cls, entry: T) -> FetchedArticle:
        """Entry → ``FetchedArticle`` の写像 (total)。"""
        ...


type AcquirableSource[T] = ArticleSource[T] | RssSource
