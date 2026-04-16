"""Analysis リポジトリ — analysis ドメインの DB 操作を担う。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.article_analysis import ArticleAnalysis
from app.models.article_keyword import ArticleKeyword
from app.models.category import Category
from app.models.keyword import Keyword
from app.models.news_article import NewsArticle


class AnalysisRepository:
    """記事分析と埋め込み関連の SQL 操作をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_article_id(self, article_id: int) -> ArticleAnalysis | None:
        """冪等性チェック用に、既存の分析結果を検索する。"""
        stmt = select(ArticleAnalysis).where(
            ArticleAnalysis.news_article_id == article_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_article(self, article_id: int) -> NewsArticle | None:
        """ID から記事を取得する。"""
        return await self._session.get(NewsArticle, article_id)

    async def get_keywords_by_category(self) -> dict[str, list[str]] | None:
        """カテゴリ slug をキーにまとめたキーワード候補一覧を取得する。"""
        stmt = select(Category.slug, Keyword.name).join(
            Keyword,
            Keyword.category_id == Category.id,
        )
        rows = (await self._session.execute(stmt)).all()
        if not rows:
            return None
        result: dict[str, list[str]] = {}
        for slug, kw in rows:
            result.setdefault(str(slug), []).append(str(kw))
        return result

    async def save_analysis(
        self,
        analysis: ArticleAnalysis,
        keyword_names: list[str] | None,
    ) -> ArticleAnalysis:
        """分析結果とキーワード紐付けを永続化する（flush のみ、commit しない）。"""
        self._session.add(analysis)
        await self._session.flush()

        if keyword_names:
            stmt = select(Keyword).where(Keyword.name.in_(keyword_names))
            matched = (await self._session.execute(stmt)).scalars().all()
            for kw in matched:
                link = ArticleKeyword(
                    article_analysis_id=analysis.id,
                    keyword_id=kw.id,
                )
                self._session.add(link)

        return analysis

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

    async def mark_article_skipped(self, article: NewsArticle) -> None:
        """記事を恒久的にスキップ対象としてマークする。"""
        article.original_content = None
        article.skip_content_fetch = True
        self._session.add(article)
