"""Extraction リポジトリ — DiscoveredArticle ルックアップと Article 永続化。

ingestion 側にも同名の ``DiscoveredArticleRepository`` が存在するが、
責務（URL 重複排除 vs 抽出対象ルックアップ）が異なるため名前空間で分離し
同名のまま扱う。各 Repo は Service 内部からのみ利用される想定。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import select

from app.collection.extraction.extractor import ExtractedContent
from app.models.article import Article
from app.models.discovered_article import DiscoveredArticle


class DiscoveredArticleRepository:
    """抽出対象となる ``DiscoveredArticle`` のルックアップを担う。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find(self, discovered_article_id: int) -> DiscoveredArticle | None:
        """ID で DiscoveredArticle を 1 件取得する。

        既存 Article の有無判定用に ``article`` リレーションを事前ロードする。
        「抽出済み / 未抽出 / 異常」というビジネス解釈は Service の責務。
        """
        stmt = (
            select(DiscoveredArticle)
            .where(DiscoveredArticle.id == discovered_article_id)
            .options(selectinload(DiscoveredArticle.article))
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


class ArticleRepository:
    """抽出済みコンテンツから ``Article`` 行を作成する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def create(self, discovered_article_id: int, content: ExtractedContent) -> Article:
        """Article をセッションに追加して返す（commit / refresh は呼び出し側）。"""
        article = Article(
            discovered_article_id=discovered_article_id,
            original_title=content.title,
            original_content=content.body,
            published_at=(
                content.published_at.value if content.published_at is not None else None
            ),
        )
        self._session.add(article)
        return article
