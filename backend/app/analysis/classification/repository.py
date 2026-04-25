"""AnalysisRepository — Stage 2 Classified の永続化と読み出し。

責務:

- ``save``: ``AnalysisDraft`` を永続化し、DB が付与した identity
  (``PersistedAnalysisId``) を返す。Entity の組み立ては呼び出し側
  (``Analysis.from_draft``) に任せる。
- ``find_by_extraction_id``: ORM 行をドメイン Entity (``Analysis``) として復元する。
- Topic 解決 (``find_or_create_topic`` / ``get_existing_topics_by_category`` /
  ``get_category_id_by_slug``): 物理配置の都合でここに同居しているが、Topic は
  独立 Aggregate である。Topic 自体のドメイン化と同時に別 Repository へ切り出す
  ことを想定 (Open Q として継続)。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.analysis.classification.domain.analysis import Analysis, AnalysisDraft
from app.analysis.domain.value_objects.topic import TopicName
from app.models.article_analysis import ArticleAnalysis
from app.models.category import Category
from app.models.topic import Topic


@dataclass(frozen=True, slots=True)
class PersistedAnalysisId:
    """永続化で DB が付与した identity。

    ``save`` の戻り値。呼び出し側はこの値と元の ``AnalysisDraft`` を
    ``Analysis.from_draft`` に渡して記録済み Entity を組み立てる。
    """

    id: int
    analyzed_at: datetime


class AnalysisRepository:
    """Stage 2 Classified の永続化に必要な DB 操作をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_extraction_id(self, extraction_id: int) -> Analysis | None:
        """extraction に紐づく分析結果を Entity として取得する。冪等性チェック兼用。"""
        stmt = select(ArticleAnalysis).where(
            ArticleAnalysis.extraction_id == extraction_id,
        )
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return self._to_domain(orm) if orm is not None else None

    async def save(
        self,
        draft: AnalysisDraft,
        *,
        extraction_id: int,
        topic_id: int,
        ai_model: str,
    ) -> PersistedAnalysisId:
        """Draft を永続化し、DB が採番した identity を返す。

        commit は呼び出し側 (Service) が行う。``analyzed_at`` は server_default
        により DB が確定させるため refresh で取得する。
        """
        orm = ArticleAnalysis(
            extraction_id=extraction_id,
            translated_title=draft.translated_title,
            summary=draft.summary,
            topic_id=topic_id,
            investor_take=draft.investor_take,
            ai_model=ai_model,
        )
        self._session.add(orm)
        await self._session.flush()
        await self._session.refresh(orm, attribute_names=["analyzed_at"])
        return PersistedAnalysisId(id=orm.id, analyzed_at=orm.analyzed_at)

    async def get_existing_topics_by_category(
        self,
    ) -> dict[str, list[tuple[str, str]]]:
        """カテゴリ別に全 Topic を ``(name, label_ja)`` ペアで取得する。

        AI 再利用判定の精度向上のため、シードと AI 動的生成の両方を漏れなく
        提示する。シード topic は analyses 件数 0 でも提示するため LEFT JOIN 集計。
        topic が存在しない場合は空 dict を返す (None ではない — 呼び出し側の
        分岐負担を減らす)。
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
        result: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for slug, topic_name, label_ja, _ in rows:
            result[str(slug)].append((str(topic_name), str(label_ja)))
        return dict(result)

    async def get_category_id_by_slug(self, slug: str) -> int | None:
        """カテゴリ slug から ID を取得する。"""
        stmt = select(Category.id).where(Category.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_or_create_topic(
        self,
        name: TopicName,
        label_ja: str,
        category_id: int,
    ) -> int:
        """Topic を検索し、なければ作成して ID を返す。

        新規作成時のみ AI 出力の ``label_ja`` を採用する。既存 topic の
        ``label_ja`` は DB 値を信頼し更新しない (シード手動キュレーション値の
        ブレを避けるため)。並行分析時の UNIQUE 制約違反は ON CONFLICT DO NOTHING
        で吸収する。
        """
        insert_stmt = (
            pg_insert(Topic)
            .values(name=name, label_ja=label_ja, category_id=category_id)
            .on_conflict_do_nothing(constraint="uq_topics_name_category_id")
        )
        await self._session.execute(insert_stmt)
        await self._session.flush()

        select_stmt = select(Topic.id).where(
            Topic.name == name,
            Topic.category_id == category_id,
        )
        return (await self._session.execute(select_stmt)).scalar_one()

    @staticmethod
    def _to_domain(orm: ArticleAnalysis) -> Analysis:
        """ORM から記録済み Entity へ復元する。"""
        return Analysis(
            id=orm.id,
            extraction_id=orm.extraction_id,
            translated_title=orm.translated_title,
            summary=orm.summary,
            topic_id=orm.topic_id,
            investor_take=orm.investor_take,
            ai_model=orm.ai_model,
            analyzed_at=orm.analyzed_at,
        )
