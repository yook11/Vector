"""Analysis リポジトリ — Stage 3 (embedding) のみが利用するレガシー Repository。

Stage 2 (分類) は ``app/analysis/classification/repository.py`` のドメイン版に
移行済み。本ファイルは ``app/analysis/embedding_service.py`` が依存している
``find_by_extraction_id`` / ``save_embedding`` のためにのみ残置されている。

Stage 3 のドメイン化と同時に削除予定。HTTP handler / router 層からの直接 import
は禁止 (Stage 2 の旧 import は PR-D で全て除去済み)。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.article_analysis import ArticleAnalysis


class AnalysisRepository:
    """記事分析と埋め込み関連の SQL 操作をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_extraction_id(self, extraction_id: int) -> ArticleAnalysis | None:
        """冪等性チェック用に、extraction に紐づく分析結果を検索する。"""
        stmt = select(ArticleAnalysis).where(
            ArticleAnalysis.extraction_id == extraction_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def save_embedding(
        self,
        analysis: ArticleAnalysis,
        vector: list[float],
        model: str,
    ) -> None:
        """既存の analysis に埋め込みベクトルを保存する。"""
        analysis.embedding = vector
        analysis.embedding_model = model
        self._session.add(analysis)
