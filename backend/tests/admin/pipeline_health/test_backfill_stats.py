"""管理画面の救済対象集計が期間と工程の完了状態を守ることを確認する。"""

from datetime import UTC, datetime, timedelta

import pytest

from app.admin.pipeline_health.repository import PipelineHealthRepository
from app.audit.domain.event import Stage
from app.models.backfill_exclusion import (
    AssessmentBackfillExclusion,
    EmbeddingBackfillExclusion,
)
from app.models.curation_noise import CurationNoise
from app.models.out_of_scope_article_record import OutOfScopeArticleRecord
from tests.backfill.helpers import complete_target, seed_target


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["curation", "assessment", "embedding"])
async def test_stats_include_only_unfinished_targets_in_window(
    db_session, sample_source, sample_categories, stage
):
    """期間内の未完了対象だけから件数と元記事の最古時刻を集計する。"""
    now = datetime(2026, 9, 14, 12, tzinfo=UTC)
    lower = now - timedelta(days=7)
    upper = now - timedelta(minutes=30)
    for created_at in [
        lower - timedelta(microseconds=1),
        lower,
        upper - timedelta(microseconds=1),
        upper,
    ]:
        await seed_target(
            db_session, sample_source, sample_categories[0], stage, created_at
        )
    done = await seed_target(
        db_session, sample_source, sample_categories[0], stage, lower
    )
    await complete_target(db_session, done, stage, sample_categories[0])
    excluded = await seed_target(
        db_session, sample_source, sample_categories[0], stage, lower
    )
    if stage == "curation":
        db_session.add(
            CurationNoise(
                analyzable_article_id=excluded.article_id,
                translated_title="noise",
                summary="noise",
            )
        )
    elif stage == "assessment":
        db_session.add(
            AssessmentBackfillExclusion(
                curation_id=excluded.target_id,
                reason_code="backfill_assessment_aged_out",
            )
        )
        out_of_scope = await seed_target(
            db_session, sample_source, sample_categories[0], stage, lower
        )
        db_session.add(
            OutOfScopeArticleRecord(
                curation_id=out_of_scope.target_id,
                translated_title="title",
                summary="summary",
                investor_take="take",
            )
        )
    else:
        db_session.add(
            EmbeddingBackfillExclusion(
                analyzed_article_id=excluded.target_id,
                reason_code="backfill_embedding_aged_out",
            )
        )
    await db_session.flush()

    stats = await PipelineHealthRepository(db_session).backfill_stats(
        created_before=upper, created_after=lower
    )

    assert stats[Stage(stage)] == (2, lower)
