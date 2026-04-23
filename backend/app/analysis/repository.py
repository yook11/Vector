"""Analysis リポジトリ — Stage 2 以降（分類・埋め込み）の DB 操作を担う。"""

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
from app.models.topic import Topic


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

    async def save_analysis(self, analysis: ArticleAnalysis) -> ArticleAnalysis:
        """分析結果を永続化する（flush のみ、commit しない）。"""
        self._session.add(analysis)
        await self._session.flush()
        return analysis

    async def get_existing_topics_by_category(
        self,
    ) -> dict[str, list[tuple[str, str]]] | None:
        """カテゴリ別に全 Topic を取得する（(name, label_ja) のペア）。

        AI 再利用判定の精度向上のため、シードと AI 動的生成の両方を
        漏れなく提示する（記事数降順、上限なし）。シード topic は
        analyses 件数が 0 でも提示対象に含めるため LEFT JOIN 集計する。
        """
        stmt = (
            select(
                Category.slug,
                Topic.name,
                Topic.label_ja,
                func.count(ArticleAnalysis.id).label("analysis_count"),
            )
            .join(Topic, Topic.category_id == Category.id)
            .outerjoin(ArticleAnalysis, ArticleAnalysis.topic_id == Topic.id)
            .group_by(Category.slug, Topic.id, Topic.name, Topic.label_ja)
            .order_by(Category.slug, func.count(ArticleAnalysis.id).desc())
        )
        rows = (await self._session.execute(stmt)).all()
        if not rows:
            return None

        result: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for slug, topic_name, label_ja, _ in rows:
            result[str(slug)].append((str(topic_name), str(label_ja)))
        return dict(result)

    async def get_category_id_by_slug(self, slug: str) -> int | None:
        """カテゴリ slug から ID を取得する。"""
        stmt = select(Category.id).where(Category.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_or_create_topic(
        self, name: str, label_ja: str, category_id: int
    ) -> int:
        """Topic を検索し、なければ作成して ID を返す。

        新規作成時のみ AI 出力の label_ja を採用する。既存 topic の
        label_ja は DB 値を信頼し更新しない（シード手動キュレーション値の
        ブレを避けるため）。並行分析時の UNIQUE 制約違反に対しては
        ON CONFLICT DO NOTHING で対応する。
        """
        topic_name = TopicName(name)

        insert_stmt = (
            pg_insert(Topic)
            .values(name=topic_name, label_ja=label_ja, category_id=category_id)
            .on_conflict_do_nothing(constraint="uq_topics_name_category_id")
        )
        await self._session.execute(insert_stmt)
        await self._session.flush()

        select_stmt = select(Topic.id).where(
            Topic.name == topic_name,
            Topic.category_id == category_id,
        )
        topic_id = (await self._session.execute(select_stmt)).scalar_one()
        return topic_id

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

    async def get_entities_by_extraction_id(
        self, extraction_id: int
    ) -> list[ArticleEntity]:
        """Stage 2 の入力用に extraction 配下のエンティティを取得する。"""
        stmt = select(ArticleEntity).where(
            ArticleEntity.article_extraction_id == extraction_id
        )
        return list((await self._session.execute(stmt)).scalars().all())
