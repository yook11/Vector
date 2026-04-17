"""ソースレジストリ — SourceName からフェッチャーへのマッピング。

全ソースを登録し、ソース名に応じた SourceFetcher を返す。
RSS ソースは同一 RssFetcher インスタンスを共有し、
API ソースは個別のフェッチャーを持つ。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.article_persister import SourceFetchResult
from app.domain.news_source import SourceName
from app.models.news_source import NewsSource


@runtime_checkable
class SourceFetcher(Protocol):
    """ソースフェッチャーの共通インターフェース。"""

    async def fetch(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        source: NewsSource,
    ) -> SourceFetchResult: ...


def _build_registry() -> dict[SourceName, SourceFetcher]:
    """全ソースのフェッチャーを登録する。"""
    from app.collection.alpha_vantage import AlphaVantageFetcher
    from app.collection.hacker_news import HackerNewsFetcher
    from app.collection.rss_fetcher import RssFetcher

    rss = RssFetcher()

    return {
        # RSS ソース（同一インスタンスを共有）
        SourceName("TechCrunch"): rss,
        SourceName("The Verge"): rss,
        SourceName("Ars Technica"): rss,
        SourceName("Wired"): rss,
        SourceName("MIT Technology Review"): rss,
        SourceName("VentureBeat"): rss,
        SourceName("The Register"): rss,
        # API ソース（個別フェッチャー）
        SourceName("Hacker News"): HackerNewsFetcher(),
        SourceName("Alpha Vantage"): AlphaVantageFetcher(),
    }


_REGISTRY: dict[SourceName, SourceFetcher] | None = None


def get_fetcher(source: NewsSource) -> SourceFetcher:
    """ソースに対応するフェッチャーを返す。

    未登録のソース名の場合は KeyError を送出する。

    Args:
        source: フェッチャーを取得する対象のソース。

    Returns:
        SourceFetcher の実装。

    Raises:
        KeyError: ソース名がレジストリに登録されていない場合。
    """
    global _REGISTRY  # noqa: PLW0603
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY[source.name]
