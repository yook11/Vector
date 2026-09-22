"""期限切れの未完了データと監査を一記事ずつ原子的に整理する。"""

from datetime import UTC, datetime

import structlog

from app.audit.stages.assessment import AssessmentAuditRepository
from app.audit.stages.completion import ArticleCompletionAuditRepository
from app.audit.stages.curation import CurationAuditRepository
from app.audit.stages.embedding import EmbeddingAuditRepository
from app.backfill.metrics import age_delete_batch_size_histogram, age_deleted_counter
from app.backfill.policy import (
    ASSESSMENTS_LIMIT,
    COMPLETIONS_LIMIT,
    CURATIONS_DELETE_LIMIT,
    EMBEDDINGS_LIMIT,
)
from app.backfill.repository import PipelineBacklog
from app.collection.article_completion.consumer_repository import (
    ArticleCompletionConsumerRepository,
)
from app.collection.persistence.analyzable_article_repository import (
    AnalyzableArticleRepository,
)
from app.db.session import SessionFactory
from app.models.backfill_exclusion import (
    AssessmentBackfillExclusion,
    BackfillExclusionReason,
    EmbeddingBackfillExclusion,
)

logger = structlog.get_logger(__name__)


async def delete_aged_out_curations(
    session_factory: SessionFactory,
    *,
    created_before: datetime,
) -> int:
    """完了処理と競合した記事を再確認し、未完了だけを削除する。"""
    async with session_factory() as session:
        ids = await PipelineBacklog(session).analyzable_article_ids_aged_out_curation(
            created_before=created_before,
            limit=CURATIONS_DELETE_LIMIT,
        )
    deleted = 0
    for target_id in ids:
        async with session_factory() as session, session.begin():
            article_id = await PipelineBacklog(session).lock_aged_out_curation(
                target_id,
                created_before=created_before,
            )
            if article_id is None:
                continue
            await CurationAuditRepository(session).append_backfill_curation_aged_out(
                analyzable_article_id=article_id,
            )
            await AnalyzableArticleRepository(session).delete_by_id(article_id)
        deleted += 1
    age_delete_batch_size_histogram.record(deleted, attributes={"stage": "curation"})
    if deleted:
        age_deleted_counter.add(deleted, attributes={"stage": "curation"})
        logger.info("backfill_curations_aged_out", deleted=deleted)
    return deleted


async def exclude_aged_out_assessments(
    session_factory: SessionFactory,
    *,
    created_before: datetime,
) -> int:
    """投資判定の部分成果を残し、期限切れの未判定だけを救済対象から除く。"""
    async with session_factory() as session:
        ids = await PipelineBacklog(session).curation_ids_aged_out_assessment(
            created_before=created_before,
            limit=ASSESSMENTS_LIMIT,
        )
    excluded = 0
    for curation_id in ids:
        async with session_factory() as session, session.begin():
            article_id = await PipelineBacklog(session).lock_aged_out_assessment(
                curation_id,
                created_before=created_before,
            )
            if article_id is None:
                continue
            session.add(
                AssessmentBackfillExclusion(
                    curation_id=curation_id,
                    reason_code=BackfillExclusionReason.ASSESSMENT_AGED_OUT.value,
                )
            )
            await AssessmentAuditRepository(
                session
            ).append_backfill_assessment_aged_out(
                curation_id=curation_id,
                analyzable_article_id=article_id,
            )
        excluded += 1
    if excluded:
        logger.info("backfill_assessments_aged_out_excluded", excluded=excluded)
    return excluded


async def exclude_aged_out_embeddings(
    session_factory: SessionFactory,
    *,
    created_before: datetime,
) -> int:
    """判定成果を残し、期限切れでembeddingがないものだけを救済対象から除く。"""
    async with session_factory() as session:
        ids = await PipelineBacklog(session).analyzed_article_ids_aged_out_embedding(
            created_before=created_before,
            limit=EMBEDDINGS_LIMIT,
        )
    excluded = 0
    for analyzed_article_id in ids:
        async with session_factory() as session, session.begin():
            article_id = await PipelineBacklog(session).lock_aged_out_embedding(
                analyzed_article_id,
                created_before=created_before,
            )
            if article_id is None:
                continue
            session.add(
                EmbeddingBackfillExclusion(
                    analyzed_article_id=analyzed_article_id,
                    reason_code=BackfillExclusionReason.EMBEDDING_AGED_OUT.value,
                )
            )
            await EmbeddingAuditRepository(session).append_backfill_embedding_aged_out(
                analyzed_article_id=analyzed_article_id,
                analyzable_article_id=article_id,
            )
        excluded += 1
    if excluded:
        logger.info("backfill_embeddings_aged_out_excluded", excluded=excluded)
    return excluded


async def close_aged_out_completions(
    session_factory: SessionFactory,
    *,
    created_before: datetime,
) -> int:
    """期限切れの未完成行だけをclosedにし、補完の打ち切りを確定する。"""
    async with session_factory() as session:
        ids = await PipelineBacklog(session).incomplete_article_ids_aged_out_completion(
            created_before=created_before,
            limit=COMPLETIONS_LIMIT,
        )
    closed = 0
    for incomplete_article_id in ids:
        async with session_factory() as session, session.begin():
            backlog = PipelineBacklog(session)
            locked = await backlog.lock_aged_out_completion(
                incomplete_article_id,
                created_before=created_before,
            )
            if locked is None:
                continue
            source_id, source_name = locked
            await ArticleCompletionConsumerRepository(session).close_nonclosed(
                incomplete_article_id, now=datetime.now(UTC)
            )
            await ArticleCompletionAuditRepository(
                session
            ).append_backfill_completion_aged_out(
                incomplete_article_id=incomplete_article_id,
                source_id=source_id,
                source_name=str(source_name),
            )
        closed += 1
    if closed:
        logger.info("backfill_completions_aged_out_closed", closed=closed)
    return closed
