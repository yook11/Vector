"""``fetch_articles`` — Source 宣言を解釈して ``FetchedArticle`` を流す engine。

source の 4 宣言 (``read`` / ``in_scope`` / ``select`` / ``map_entry``) を
**取得 → スコープ → 整序/制限/dedup → 写像** の順に合成する汎用 engine。convert は
含まない (service に残置)。state を持たないので自由関数。

``read`` の ``ExternalFetchError`` (Reader 由来) は **catch せず素通り**させる。
翻訳は service の責務 (写像は構造的 total ゆえ fetch 側に funnel は不要)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.article_collection.fetched_article import FetchedArticle
from app.collection.article_collection.tools.reader_tools import ReaderTools
from app.collection.sources.article_source import ArticleSource


async def fetch_articles[T](
    source: ArticleSource[T], tools: ReaderTools
) -> AsyncIterator[FetchedArticle]:
    """``source`` の宣言を駆動し ``FetchedArticle`` を yield する。

    ``source`` は registry の class object をそのまま受ける (classmethod 経由で解決)。
    scope → select の順 (scope 後の列に dedup/limit を効かせる)。
    """
    entries = await source.read(tools)
    for entry in source.select([e for e in entries if source.in_scope(e)]):
        yield source.map_entry(entry)
