"""InScopeRepository — Stage 4 in-scope 評価結果の永続化と読み出し。

責務:
- ``exists_for_extraction``: cheap な exists 判定 (Pattern A' の `try_advance_from`
  precondition チェック用)
- ``find_by_extraction_id``: extraction_id 経由で既存 Entity を取得 (reconcile
  cron / 検査経路で使用)
- ``find_by_id``: PK 検索 (Stage 5 経路 backfill_embeddings から使用)
- ``save``: AI 境界型 ``InScope`` + Stage 3 由来の translated_title / summary を
  受けて ``INSERT ... ON CONFLICT (extraction_id) DO NOTHING RETURNING ...`` で
  永続化する。category slug → id 解決を内部に閉じ、未登録 slug は
  ``AssessmentCategoryMissingError`` で fail-fast。race 敗北時は ``None`` を返し、
  Service は短絡する (再収集は reconcile cron が担う)。

注 (PR3.5-d.0): Domain Entity ``InScopeAssessment`` と ORM クラス ``InScopeAssessment``
が同名のため、本ファイル内では ORM 側を ``InScopeAssessmentORM`` alias で import
して衝突回避する。Repository の caller には Domain Entity が返るため alias は外に
漏れない。
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.analysis.assessment.ai.schema import InScope
from app.analysis.assessment.domain.in_scope import InScopeAssessment
from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.errors import AssessmentCategoryMissingError
from app.models.category import Category
from app.models.in_scope_assessment import InScopeAssessment as InScopeAssessmentORM


class InScopeRepository:
    """Stage 4 in-scope 評価結果の永続化に必要な DB 操作をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists_for_extraction(self, extraction_id: int) -> bool:
        """`try_advance_from` 用 cheap exists 判定 (extraction_id 単位)。"""
        stmt = (
            select(InScopeAssessmentORM.id)
            .where(InScopeAssessmentORM.extraction_id == extraction_id)
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def find_by_extraction_id(
        self, extraction_id: int
    ) -> InScopeAssessment | None:
        """extraction_id 経由で既存の評価結果を取得する (reconcile cron 用)。"""
        stmt = select(InScopeAssessmentORM).where(
            InScopeAssessmentORM.extraction_id == extraction_id,
        )
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return self._to_domain(orm) if orm is not None else None

    async def find_by_id(self, assessment_id: int) -> InScopeAssessment | None:
        """PK 検索 (Stage 5 経路 backfill_embeddings から使用)。"""
        stmt = select(InScopeAssessmentORM).where(
            InScopeAssessmentORM.id == assessment_id
        )
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return self._to_domain(orm) if orm is not None else None

    async def save(
        self,
        in_scope: InScope,
        *,
        ready: ReadyForAssessment,
        ai_model: str,
    ) -> InScopeAssessment | None:
        """AI 境界型 + ``ReadyForAssessment`` (Stage 3 由来 snapshot) を受けて
        永続化する。

        ``extraction_id`` / ``translated_title`` / ``summary`` は ``ready`` から取り出す
        (Service 側の詰め替えを廃して ``AssessmentAuditRepository.append_*`` と
        signature を対称化)。

        category slug → id 解決を内部化し、未登録 slug は
        ``AssessmentCategoryMissingError`` で fail-fast (Layer 2-B 業務 invariant)。
        commit は呼び出し側 (Service) が行う。``analyzed_at`` は server_default で
        DB が確定させ RETURNING で受け取る。

        Returns:
            成功時: 永続化された ``InScopeAssessment`` Entity (id / analyzed_at は
            DB 値、その他は引数値)
            race 敗北時 (期待した extraction_id への UNIQUE 違反): ``None``
            (敗者は audit を焼かず短絡する — 勝者 task が自身の audit を焼く、
            audit actor SSoT 維持)

        Raises:
            ``AssessmentCategoryMissingError``: AI が catalog 未登録の slug を返した

        spec §4.3.1 に従い `index_elements=["extraction_id"]` で index を明示し、
        他の制約違反 (FK / CHECK / NOT NULL) は例外として上に上げる。
        """
        category_id = await self._get_category_id_by_slug(in_scope.category.value)
        if category_id is None:
            raise AssessmentCategoryMissingError(
                f"AI returned unknown category slug: {in_scope.category.value!r}"
            )

        stmt = (
            pg_insert(InScopeAssessmentORM)
            .values(
                extraction_id=ready.extraction_id,
                translated_title=ready.translated_title,
                summary=ready.summary,
                topic=in_scope.topic,
                category_id=category_id,
                investor_take=in_scope.investor_take,
                ai_model=ai_model,
            )
            .on_conflict_do_nothing(index_elements=["extraction_id"])
            .returning(InScopeAssessmentORM.id, InScopeAssessmentORM.analyzed_at)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        return InScopeAssessment(
            id=row.id,
            extraction_id=ready.extraction_id,
            translated_title=ready.translated_title,
            summary=ready.summary,
            topic=in_scope.topic,
            category_id=category_id,
            investor_take=in_scope.investor_take,
            ai_model=ai_model,
            analyzed_at=row.analyzed_at,
        )

    async def _get_category_id_by_slug(self, slug: str) -> int | None:
        """カテゴリ slug から ID を取得する (Repository 内部使用)。"""
        stmt = select(Category.id).where(Category.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def _to_domain(orm: InScopeAssessmentORM) -> InScopeAssessment:
        """ORM から記録済み Entity へ復元する。"""
        return InScopeAssessment(
            id=orm.id,
            extraction_id=orm.extraction_id,
            translated_title=orm.translated_title,
            summary=orm.summary,
            topic=orm.topic,
            category_id=orm.category_id,
            investor_take=orm.investor_take,
            ai_model=orm.ai_model,
            analyzed_at=orm.analyzed_at,
        )
