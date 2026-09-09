"""RSS宣言を実行して共通の記事材料を取得する。"""

from collections.abc import AsyncIterator

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.reader.rss_reader import RssReader
from app.collection.sources.rss_acquisition import RssAcquisition


class RssFetcher:
    def __init__(self, reader: RssReader) -> None:
        self._reader = reader

    async def fetch(
        self, acquisition: RssAcquisition, *, source_name: str
    ) -> AsyncIterator[FetchedArticle]:
        # 複数フィードは部分失敗の契約を実装するスライス3まで拒否する。
        if len(acquisition.feeds) != 1:
            raise ValueError("multiple RSS feeds are not supported yet")
        entries = await self._reader.fetch(
            endpoint_url=acquisition.feeds[0],
            source_name=source_name,
            parse_mode=acquisition.parse_mode,
        )
        for entry in entries:
            yield FetchedArticle(
                title=entry.title,
                url=entry.link,
                body=None,
                published_at=entry.published,
            )
