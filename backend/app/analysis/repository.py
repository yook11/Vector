"""Analysis リポジトリ — analysis ドメインの DB 操作を担う。"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.domain.topic import TopicName
from app.models.article_analysis import ArticleAnalysis
from app.models.article_entity import ArticleEntity
from app.models.category import Category
from app.models.news_article import NewsArticle
from app.models.topic import Topic


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

    async def get_existing_topics_by_category(
        self,
    ) -> dict[str, list[str]] | None:
        """カテゴリ別に既存 Topic を記事数降順で取得する（各上位30件）。"""
        stmt = (
            select(Category.slug, Topic.name)
            .join(Topic, Topic.category_id == Category.id)
            .join(ArticleAnalysis, ArticleAnalysis.topic_id == Topic.id)
            .group_by(Category.slug, Topic.id, Topic.name)
            .order_by(Category.slug, func.count().desc())
        )
        rows = (await self._session.execute(stmt)).all()
        if not rows:
            return None

        result: dict[str, list[str]] = defaultdict(list)
        for slug, topic_name in rows:
            topics = result[str(slug)]
            if len(topics) < 30:
                topics.append(str(topic_name))
        return dict(result)

    async def get_category_id_by_slug(self, slug: str) -> int | None:
        """カテゴリ slug から ID を取得する。"""
        stmt = select(Category.id).where(Category.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_or_create_topic(self, name: str, category_id: int) -> int:
        """Topic を検索し、なければ作成して ID を返す。

        並行分析時の UNIQUE 制約違反に対して ON CONFLICT DO NOTHING で対応する。
        """
        topic_name = TopicName(name)

        # ON CONFLICT DO NOTHING で INSERT を試みる
        insert_stmt = (
            pg_insert(Topic)
            .values(name=topic_name, category_id=category_id)
            .on_conflict_do_nothing(constraint="uq_topics_name_category_id")
        )
        await self._session.execute(insert_stmt)
        await self._session.flush()

        # INSERT が成功しても競合でも、SELECT で取得する
        select_stmt = select(Topic.id).where(
            Topic.name == topic_name,
            Topic.category_id == category_id,
        )
        topic_id = (await self._session.execute(select_stmt)).scalar_one()
        return topic_id

    async def save_analysis(self, analysis: ArticleAnalysis) -> ArticleAnalysis:
        """分析結果を永続化する（flush のみ、commit しない）。"""
        self._session.add(analysis)
        await self._session.flush()
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

    async def get_entities_by_analysis_id(
        self, analysis_id: int
    ) -> list[ArticleEntity]:
        """Stage 2 の入力用にエンティティを取得する。"""
        stmt = select(ArticleEntity).where(
            ArticleEntity.article_analysis_id == analysis_id
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def mark_article_skipped(self, article: NewsArticle) -> None:
        """記事を恒久的にスキップ対象としてマークする。"""
        article.original_content = None
        article.skip_content_fetch = True
        self._session.add(article)
