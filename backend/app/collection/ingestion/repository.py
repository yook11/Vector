"""Ingestion リポジトリ — DiscoveredArticle の永続化操作。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.domain.safe_url import SafeUrl
from app.models.discovered_article import DiscoveredArticle


class DiscoveredArticleRepository:
    """``DiscoveredArticle`` に対する DB 操作をカプセル化する。"""

    _URL_CHUNK_SIZE = 500

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fetch_existing_urls(self, urls: list[SafeUrl]) -> set[SafeUrl]:
        """指定 URL の中で既に DB に存在するものを返す。

        PostgreSQL の IN-clause パラメタ上限を避けるため ``_URL_CHUNK_SIZE`` 件ずつ
        チャンク分割して問い合わせる。
        """
        existing: set[SafeUrl] = set()
        for i in range(0, len(urls), self._URL_CHUNK_SIZE):
            chunk = urls[i : i + self._URL_CHUNK_SIZE]
            stmt = select(DiscoveredArticle.original_url).where(
                DiscoveredArticle.original_url.in_(chunk)
            )
            rows = await self._session.execute(stmt)
            existing.update(row[0] for row in rows.all())
        return existing

    def add(self, discovered: DiscoveredArticle) -> None:
        """新規 DiscoveredArticle をセッションに追加する（commit は呼び出し側）。"""
        self._session.add(discovered)
