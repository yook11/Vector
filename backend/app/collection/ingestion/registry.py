"""ソースレジストリ — SourceName からフェッチャーへのマッピング。

全ソースを登録し、ソース名に応じた SourceFetcher を返す。
各ソースは固有のフェッチャーインスタンスを持つ。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import httpx

from app.collection.domain.value_objects.source import SourceName
from app.collection.ingestion.domain import ArticleCandidate
from app.models.news_source import NewsSource
from app.shared.value_objects.safe_url import SafeUrl


@runtime_checkable
class SourceFetcher(Protocol):
    """ソースフェッチャーの共通インターフェース。

    Fetcher は外部配信形式を ``ArticleCandidate`` に正規化して返すだけで、
    DB 永続化は ``SourceFetchService`` 側の責務とする。
    """

    async def fetch(
        self,
        client: httpx.AsyncClient,
        source: NewsSource,
    ) -> dict[SafeUrl, ArticleCandidate]: ...


def _build_registry() -> dict[SourceName, SourceFetcher]:
    """全ソースのフェッチャーを登録する。"""
    from app.collection.ingestion.fetchers.hacker_news import HackerNewsFetcher

    return {
        # collection-acquisition-redesign Phase 1d 完了時点で RSS 18 ソース
        # (VentureBeat / TechCrunch / The Quantum Insider / Krebs on Security /
        # Spaceflight Now / NASA / IEEE Spectrum / Microsoft Research /
        # ITmedia AI+ / ITmedia NEWS / MONOist / EE Times Japan / Engadget /
        # FierceBiotech / JPCERT/CC / CleanTechnica / Electrek / SpaceNews /
        # The Register) は ``strategy.NEW_ROUTE_FETCHERS`` 経由の新 Protocol
        # Fetcher に移行済み。本 registry には API Pattern の Hacker News
        # 1 ソースのみが残る。PR-1e (HN 新 Protocol 化) で本ファイルごと
        # 削除し、Strangler 経路を完全撤去する予定。
        SourceName("Hacker News"): HackerNewsFetcher(),
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
