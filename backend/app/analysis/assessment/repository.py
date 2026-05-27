"""AssessmentRepository — Stage 4 判定結果 (in-scope / out-of-scope) の永続化。

責務:

- ``try_load_for_assessment``: 「行存在 + 両 assessment 未生成 + audit 用参照値
  fetch」を 1 query で atomic に判定し、満たす場合のみ ``ReadyForAssessment`` を
  直接構築して返す (案 3 = 厚い Ready)。Domain 層
  ``ReadyForAssessment.try_advance_from`` は本 method への thin delegate。
- ``save_in_scope`` / ``save_out_of_scope``: AI 境界型 ``InScope`` / ``OutOfScope``
  を内包する ``AssessmentCall`` を受け、
  ``INSERT ... ON CONFLICT (curation_id) DO NOTHING RETURNING id`` で
  永続化する。両 method とも DB 採番 ``id`` (``int | None``) を返し、race 敗北時
  (UNIQUE 違反) は ``None``。in-scope は category slug → id 解決を内部に閉じ、
  未登録 slug は ``AssessmentCategoryMissingError`` で fail-fast。
  Service は ``id`` のみで race 検出 + Stage 5 chain を行う (再収集は
  reconcile cron が担う)。

設計方針 (2026-05-12 確定、案 3): 旧 Pattern A' 時代に分かれていた cheap exists
判定 (in/out scope 2 query) と audit 用参照値 fetch (2-hop 逆引き) を 1 query
(``try_load_for_assessment``) に統合。Ready は **処理に必要な値の全揃え** を
構造保証する厚い型として運ばれ、Repository は Ready 構築に必要な情報を
1 回の DB 往復で完結させる責務を持つ。
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.domain.result import InScope, OutOfScope
from app.analysis.assessment.errors import AssessmentCategoryMissingError
from app.models.article import Article
from app.models.article_curation import ArticleCuration
from app.models.category import Category
from app.models.in_scope_assessment import InScopeAssessment
from app.models.news_source import NewsSource
from app.models.out_of_scope_assessment import OutOfScopeAssessment

logger = structlog.get_logger(__name__)


class AssessmentRepository:
    """Stage 4 判定結果 (in-scope / out-of-scope) の永続化 + Ready 構築判定。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- Ready 構築 (try_advance_from precondition + audit 参照値) -------------

    async def try_load_for_assessment(
        self, curation_id: int
    ) -> ReadyForAssessment | None:
        """`ReadyForAssessment.try_advance_from` 用 atomic ロード。

        1 query で「curation 行存在 + 両 assessment 未生成」を判定し、
        満たす場合のみ assessor 入力 (``translated_title`` / ``summary``) と
        audit 参照値 (``article_id`` / ``source_name``) を取得して厚い Ready を
        構築して返す。

        Returns:
            進める場合: precondition を満たし、audit 参照値も含む
                ``ReadyForAssessment``
            進めない場合: ``None`` (curation 不在 / 既 in-scope / 既 out-of-scope)
        """
        stmt = (
            select(
                ArticleCuration.translated_title,
                ArticleCuration.summary,
                ArticleCuration.article_id,
                NewsSource.name,
            )
            .join(Article, Article.id == ArticleCuration.article_id)
            .outerjoin(NewsSource, NewsSource.id == Article.source_id)
            .outerjoin(
                InScopeAssessment,
                InScopeAssessment.curation_id == ArticleCuration.id,
            )
            .outerjoin(
                OutOfScopeAssessment,
                OutOfScopeAssessment.curation_id == ArticleCuration.id,
            )
            .where(
                ArticleCuration.id == curation_id,
                InScopeAssessment.id.is_(None),
                OutOfScopeAssessment.id.is_(None),
            )
            .limit(1)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        translated_title, summary, article_id, source_name = row
        return ReadyForAssessment(
            curation_id=curation_id,
            translated_title=translated_title,
            summary=summary,
            article_id=article_id,
            source_name=str(source_name) if source_name is not None else None,
        )

    # --- save 経路 ------------------------------------------------------------

    async def save_in_scope(
        self,
        call: AssessmentCall[InScope],
        *,
        ready: ReadyForAssessment,
    ) -> int | None:
        """``AssessmentCall[InScope]`` を受けて in-scope assessment を永続化する。

        ``call.result`` から永続化に必要な値を直接取り出す
        (Stage 4 で起きた事実は envelope が抱え切る、
        `feedback_bc_boundary_guarantees_downstream`)。
        ``curation_id`` / ``translated_title`` / ``summary`` は ``ready`` から
        取り出す (Stage 3 由来 snapshot)。``call.model_name`` は監査 SSoT
        (``pipeline_events.payload.ai_model``) に焼くのみで業務行には INSERT
        しない (`feedback_outcome_purification`)。

        category slug → id 解決を内部化し、未登録 slug は
        ``AssessmentCategoryMissingError`` で fail-fast (Layer 2-B 業務 invariant)。
        解決後の ``category_id`` は ``in_scope_assessments.category_id`` カラムに
        INSERT するための内部使用のみで、戻り値としては外に出さない (`save_out_of_scope`
        と完全対称)。commit は呼び出し側 (Service) が行う。

        Returns:
            成功時: DB が採番した ``id``
            race 敗北時 (期待した curation_id への UNIQUE 違反): ``None``
            (敗者は audit を焼かず短絡する — 勝者 task が自身の audit を焼く、
            audit actor SSoT 維持)

        Raises:
            ``AssessmentCategoryMissingError``: AI が catalog 未登録の slug を返した
        """
        in_scope = call.result
        category_id = await self._get_category_id_by_slug(in_scope.category.value)
        if category_id is None:
            # Phase 4: 旧 message 引数廃止 (具体 slug は PII 隔離契約上 SaaS span
            # には乗せない)。slug 値は logger.warning で stdout 側に別経路で残す。
            logger.warning(
                "assessment_category_missing",
                slug=in_scope.category.value,
                curation_id=ready.curation_id,
            )
            raise AssessmentCategoryMissingError()

        stmt = (
            pg_insert(InScopeAssessment)
            .values(
                curation_id=ready.curation_id,
                translated_title=ready.translated_title,
                summary=ready.summary,
                category_id=category_id,
                investor_take=in_scope.investor_take,
                events=[e.model_dump() for e in in_scope.events],
            )
            .on_conflict_do_nothing(index_elements=["curation_id"])
            .returning(InScopeAssessment.id)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        return row.id

    async def save_out_of_scope(
        self,
        call: AssessmentCall[OutOfScope],
        *,
        ready: ReadyForAssessment,
    ) -> int | None:
        """``AssessmentCall[OutOfScope]`` を受けて out-of-scope を永続化する。

        in-scope 経路と対称: ``call.result.investor_take`` を envelope から直接
        取り出し、Stage 3 由来 snapshot (``translated_title`` / ``summary``) は
        ``ready`` から取り出す。``call.model_name`` は監査 SSoT
        (``pipeline_events.payload.ai_model``) に焼くのみで業務行には INSERT しない。
        ``out_of_scope_assessments`` には category は無いので外す。

        Returns:
            成功時: DB が採番した ``id``
            race 敗北時 (期待した curation_id への UNIQUE 違反): ``None``
        """
        out_of_scope = call.result
        stmt = (
            pg_insert(OutOfScopeAssessment)
            .values(
                curation_id=ready.curation_id,
                translated_title=ready.translated_title,
                summary=ready.summary,
                investor_take=out_of_scope.investor_take,
                events=[e.model_dump() for e in out_of_scope.events],
            )
            .on_conflict_do_nothing(index_elements=["curation_id"])
            .returning(OutOfScopeAssessment.id)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        return row.id

    async def _get_category_id_by_slug(self, slug: str) -> int | None:
        """カテゴリ slug から ID を取得する (Repository 内部使用)。"""
        stmt = select(Category.id).where(Category.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()
