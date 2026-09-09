"""取得宣言に対応する経路へ委譲する共通入口。"""

from collections.abc import AsyncIterator

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.rss_fetcher import RssFetcher
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.sources.article_source import AcquirableSource
from app.collection.sources.rss_acquisition import RssAcquisition, RssSource


async def fetch_articles[T](
    source: AcquirableSource[T], tools: ReaderTools
) -> AsyncIterator[FetchedArticle]:
    if isinstance(source, RssSource):
        if not isinstance(source.acquisition, RssAcquisition):
            raise TypeError("unsupported acquisition declaration")
        async for article in RssFetcher(tools.rss).fetch(
            source.acquisition, source_name=str(source.name)
        ):
            yield article
        return
    if hasattr(source, "acquisition"):
        raise TypeError("invalid RSS source declaration")
    entries = await source.read(tools)
    for entry in source.select([e for e in entries if source.in_scope(e)]):
        yield source.map_entry(entry)
