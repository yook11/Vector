"""Ingestion リポジトリ — DiscoveredArticle の永続化と読み出し。

責務は 2 つ:

- ``save_many``: ``DiscoveredArticleDraft`` のリストを ``discovered_articles``
  行に bulk INSERT し、DB が採番した identity (``id`` / ``discovered_at``) を
  合成した ``DiscoveredArticleEntity`` を返す。
  ``UNIQUE(original_url)`` の並行レースは
  ``INSERT ... ON CONFLICT DO NOTHING RETURNING`` で構造的に解消する
  (重複 URL は単に skip される)。
- ``find_by_url``: URL から既存 ``DiscoveredArticle`` を Entity として復元する
  (永続化の双対)。
"""

from __future__ import annotations

from typing import Final

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.ingestion.domain import (
    DiscoveredArticleDraft,
    DiscoveredArticleEntity,
)
from app.models.discovered_article import DiscoveredArticle
from app.shared.value_objects.safe_url import SafeUrl


class DiscoveredArticleRepository:
    """``DiscoveredArticle`` に対する DB 操作をカプセル化する。"""

    # 1 トランザクションあたりの save_many 上限。fetcher 1 回分の現実的な上限
    # (max_articles_per_fetch ≪ 1000) を大幅に上回る防御的キャップ。
    _SAVE_MANY_LIMIT: Final[int] = 1000

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_many(
        self, drafts: list[DiscoveredArticleDraft]
    ) -> list[DiscoveredArticleEntity]:
        """Draft のリストを bulk INSERT し、永続化された Entity を返す。

        ``ON CONFLICT (original_url) DO NOTHING RETURNING`` により、別ワーカーが
        先に同一 URL を INSERT 済みの行は skip され、新規挿入された行のみが
        Entity として返る。RETURNING の行順は保証されないため、呼び出し側は
        順序非依存で扱うこと。

        commit は呼び出し側 (Service) が行う。
        """
        if not drafts:
            return []
        if len(drafts) > self._SAVE_MANY_LIMIT:
            raise ValueError(
                f"save_many accepts at most {self._SAVE_MANY_LIMIT} drafts, "
                f"got {len(drafts)}"
            )

        stmt = (
            pg_insert(DiscoveredArticle)
            .values(
                [
                    {
                        "original_url": d.candidate.url,
                        "original_title": d.candidate.title,
                        "news_source_id": d.news_source_id,
                    }
                    for d in drafts
                ]
            )
            .on_conflict_do_nothing(index_elements=["original_url"])
            .returning(
                DiscoveredArticle.id,
                DiscoveredArticle.news_source_id,
                DiscoveredArticle.original_url,
                DiscoveredArticle.original_title,
                DiscoveredArticle.discovered_at,
            )
        )
        result = await self._session.execute(stmt)
        return [
            DiscoveredArticleEntity(
                id=row.id,
                news_source_id=row.news_source_id,
                url=row.original_url,
                title=row.original_title,
                discovered_at=row.discovered_at,
            )
            for row in result.all()
        ]

    async def find_by_url(self, url: SafeUrl) -> DiscoveredArticleEntity | None:
        """URL から既存 ``DiscoveredArticle`` を Entity として取得する。

        並行レース敗北時 (``save_many`` が一部 skip した URL) の読み戻しと、
        defense-in-depth の冪等性検証で使う。
        """
        stmt = select(DiscoveredArticle).where(DiscoveredArticle.original_url == url)
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return self._to_domain(orm) if orm is not None else None

    @staticmethod
    def _to_domain(orm: DiscoveredArticle) -> DiscoveredArticleEntity:
        """ORM から Entity への内部変換。"""
        return DiscoveredArticleEntity(
            id=orm.id,
            news_source_id=orm.news_source_id,
            url=orm.original_url,
            title=orm.original_title,
            discovered_at=orm.discovered_at,
        )
