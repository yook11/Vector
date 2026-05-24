"""``fetch_articles`` — Source 宣言を解釈して ``FetchedArticle`` を流す engine。

source の 4 宣言 (``read`` / ``in_scope`` / ``select`` / ``map_entry``) を
**取得 → スコープ → 整序/制限/dedup → 写像** の順に合成する汎用 engine。convert は
含まない (service に残置)。state を持たないので自由関数。

``read`` を通す唯一の chokepoint なので read-error 契約をここで可視化する。read は
2 系統で失敗しうる — 接続失敗 (``ExternalFetchError``) と読取失敗
(``UnreadableResponseError``) — その union だけを明示 re-raise する。それ以外が
read から漏れたら read 契約違反 (bug) で、funnel せず素通しさせ上位 catch-all が
想定外として扱う。翻訳は service の責務 (写像は構造的 total ゆえ funnel は不要)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.collection.article_collection.errors import UnreadableResponseError
from app.collection.article_collection.fetched_article import FetchedArticle
from app.collection.article_collection.tools.reader_tools import ReaderTools
from app.collection.external_fetch_errors import ExternalFetchError
from app.collection.sources.article_source import ArticleSource


async def fetch_articles[T](
    source: ArticleSource[T], tools: ReaderTools
) -> AsyncIterator[FetchedArticle]:
    """``source`` の宣言を駆動し ``FetchedArticle`` を yield する。

    ``source`` は registry の class object をそのまま受ける (classmethod 経由で解決)。
    scope → select の順 (scope 後の列に dedup/limit を効かせる)。
    """
    try:
        entries = await source.read(tools)  # read error (接続/読取) の発生点
    except (ExternalFetchError, UnreadableResponseError):
        raise  # read 失敗 = read error として明示伝播
    for entry in source.select([e for e in entries if source.in_scope(e)]):
        yield source.map_entry(entry)
