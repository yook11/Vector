"""Source 宣言を解釈して ``FetchedArticle`` を流す取得 engine。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.article_acquisition.errors import UnreadableResponseError
from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.external_fetch_errors import ExternalFetchError
from app.collection.sources.article_source import ArticleSource


async def fetch_articles[T](
    source: ArticleSource[T], tools: ReaderTools
) -> AsyncIterator[FetchedArticle]:
    """``source`` の宣言を駆動し ``FetchedArticle`` を yield する。

    read origin error はそのまま上位へ渡し、Stage 1 marker への翻訳は service が行う。
    """
    try:
        entries = await source.read(tools)
    except (ExternalFetchError, UnreadableResponseError):
        raise
    for entry in source.select([e for e in entries if source.in_scope(e)]):
        yield source.map_entry(entry)
