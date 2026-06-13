"""Stage 4 assessment の DB 読み取りと永続化を担う repository。"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.domain.ready import (
    AssessmentReadyBuildFacts,
    ReadyForAssessment,
)
from app.analysis.assessment.domain.result import (
    InScope,
    InScopeCategory,
    OutOfScope,
)
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.category import Category
from app.models.out_of_scope_article_record import OutOfScopeArticleRecord

logger = structlog.get_logger(__name__)


class CategoryEnumDatabaseMismatchError(Exception):
    """アプリ側 ``InScopeCategory`` enum と DB の ``categories`` が食い違い、enum の
    slug が DB に存在しない不変条件の破れ。

    domain marker(``AssessmentError`` 系)ではない。意図的に marker 階層の外に置き、
    failure handler の marker 分岐に当たらず ``case _:``(想定外 = ``unexpected_error``)
    に落とすことで「業務上の結果ではなく enum↔DB の不整合バグ」として扱う。
    ``missing`` は DB に欠けている slug 集合(category slug = PII ではない)。
    """

    def __init__(self, missing: set[str]) -> None:
        self.missing = missing
        super().__init__(
            f"category enum slugs missing from database: {sorted(missing)}"
        )


def missing_category_slugs(db_slugs: set[str]) -> set[str]:
    """アプリ側 ``InScopeCategory`` のうち DB の slug 集合に無いものを返す。

    純粋関数(DB に触れない)。空集合なら enum と DB が一致している。``db_slugs`` は
    DB から読んだ category slug の集合。
    """
    return {category.value for category in InScopeCategory} - db_slugs


class AssessmentRepository:
    """Domain 判断を持たず、DB 事実と保存結果だけを返す。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- Ready 構築用 DB 事実取得 --------------------------------------------

    async def load_ready_build_facts(
        self, curation_id: int
    ) -> AssessmentReadyBuildFacts | None:
        stmt = (
            select(
                ArticleCuration.id,
                ArticleCuration.analyzable_article_id,
                ArticleCuration.translated_title,
                ArticleCuration.summary,
                AnalyzedArticleRecord.id.is_not(None),
                OutOfScopeArticleRecord.id.is_not(None),
            )
            .select_from(ArticleCuration)
            .outerjoin(
                AnalyzedArticleRecord,
                AnalyzedArticleRecord.curation_id == ArticleCuration.id,
            )
            .outerjoin(
                OutOfScopeArticleRecord,
                OutOfScopeArticleRecord.curation_id == ArticleCuration.id,
            )
            .where(ArticleCuration.id == curation_id)
            .limit(1)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        (
            loaded_curation_id,
            article_id,
            translated_title,
            summary,
            has_analyzed_article,
            has_out_of_scope_article,
        ) = row
        return AssessmentReadyBuildFacts(
            curation_id=loaded_curation_id,
            article_id=article_id,
            translated_title=translated_title,
            summary=summary,
            has_analyzed_article=has_analyzed_article,
            has_out_of_scope_article=has_out_of_scope_article,
        )

    # --- save 経路 ------------------------------------------------------------

    async def save_in_scope(
        self,
        call: AssessmentCall[InScope],
        *,
        ready: ReadyForAssessment,
    ) -> int | None:
        """in-scope assessment を保存し、既存行に負けた場合は ``None`` を返す。"""
        in_scope = call.result
        category_id = await self._get_category_id_by_slug(in_scope.category.value)
        if category_id is None:
            # enum↔DB の不整合 = 不変条件の破れ。marker でない例外を投げ failure
            # handler の case _: (想定外) に落として unexpected_error として焼く。
            # 通常は起動時 assert_category_catalog_covers_enum が先に fail-fast する。
            logger.error(
                "category_enum_database_mismatch",
                slug=in_scope.category.value,
                curation_id=ready.curation_id,
            )
            raise CategoryEnumDatabaseMismatchError({in_scope.category.value})

        stmt = (
            pg_insert(AnalyzedArticleRecord)
            .values(
                curation_id=ready.curation_id,
                translated_title=ready.translated_title,
                summary=ready.summary,
                category_id=category_id,
                investor_take=in_scope.investor_take,
                key_points=[k.model_dump() for k in in_scope.key_points],
            )
            .on_conflict_do_nothing(index_elements=["curation_id"])
            .returning(AnalyzedArticleRecord.id)
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
        """out-of-scope assessment を保存し、既存行に負けた場合は ``None`` を返す。"""
        out_of_scope = call.result
        stmt = (
            pg_insert(OutOfScopeArticleRecord)
            .values(
                curation_id=ready.curation_id,
                translated_title=ready.translated_title,
                summary=ready.summary,
                investor_take=out_of_scope.investor_take,
                key_points=[k.model_dump() for k in out_of_scope.key_points],
            )
            .on_conflict_do_nothing(index_elements=["curation_id"])
            .returning(OutOfScopeArticleRecord.id)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        return row.id

    async def _get_category_id_by_slug(self, slug: str) -> int | None:
        """カテゴリ slug から ID を取得する (Repository 内部使用)。"""
        stmt = select(Category.id).where(Category.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def assert_category_catalog_covers_enum(self) -> None:
        """全 ``InScopeCategory`` が DB に在るか検証し、欠落で raise する。

        worker 起動時に 1 回呼ぶ。DB に enum の slug が欠けていれば
        ``CategoryEnumDatabaseMismatchError`` を投げ、起動を fail-fast させる
        (enum↔DB の不整合をデプロイ時に loud に検出する)。

        ``Category.slug`` は ``CategorySlug`` VO で返るため ``.root`` で str に正規化
        してから集合演算する(VO は str と等価/同ハッシュではない)。
        """
        rows = (await self._session.execute(select(Category.slug))).scalars().all()
        db_slugs = {slug.root for slug in rows}
        missing = missing_category_slugs(db_slugs)
        if missing:
            logger.error("category_enum_database_mismatch", missing=sorted(missing))
            raise CategoryEnumDatabaseMismatchError(missing)
