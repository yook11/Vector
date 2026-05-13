"""Pattern H pre-check 用の軽量 articles 存在確認 Repository。

ingestion BC 側で ``IncompleteArticle`` を pending 化する前に
``articles.source_url`` を既知 URL として確認するための専用 query。
これはロックではなく実用上の idempotency で、HTML fetch の反復コストを
抑えるためのもの。同 tick race は ``ArticleRepository.save`` 側の
``ON CONFLICT DO NOTHING`` が構造的に吸収する。

``ArticleRepository`` (extraction BC) から分離した理由:

- 「URL を見たことがあるか」は Pattern H pre-check 固有の問い (ingestion 関心)
- ``articles`` テーブルへの最終 INSERT (extraction 責務) とは関心軸が違う
- BC 境界の依存方向を upstream (ingestion) → downstream (extraction) に揃える
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.article import Article as ArticleORM
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl


class ArticleSeenRepository:
    """``articles.source_url`` の軽量存在確認に特化した Repository。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists_by_source_url(self, source_url: CanonicalArticleUrl) -> bool:
        """``source_url`` を持つ ``articles`` 行が既に存在するかを軽量確認する。

        Pattern H ingestion の pre-check 用 (feed 再露出時に既知 URL の
        pending 化を回避し、HTML fetch の反復コストを抑える)。これはロックでは
        なく実用上の idempotency で、同 tick race は ``ArticleRepository.save``
        側の ``ON CONFLICT DO NOTHING`` が吸収する。
        """
        stmt = select(ArticleORM.id).where(ArticleORM.source_url == source_url).limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None
