"""PipelineBacklog の DB 統合テスト (年齢ウィンドウ + 子テーブル NULL の検出)。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.analyzed_article_record import AnalyzedArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.backfill_exclusion import (
    AssessmentBackfillExclusion,
    BackfillExclusionReason,
    EmbeddingBackfillExclusion,
)
from app.models.category import Category
from app.models.curation_noise import CurationNoise
from app.models.news_source import NewsSource
from app.models.out_of_scope_article_record import OutOfScopeArticleRecord
from app.queue.helpers.backlog import PipelineBacklog


async def _make_article(
    db_session: AsyncSession,
    source: NewsSource,
    *,
    url: str,
    created_at: datetime,
) -> AnalyzableArticleRecord:
    """指定 created_at の article record を作成する。"""
    article = AnalyzableArticleRecord(
        source_id=source.id,
        source_url=url,
        original_title="title",
        original_content="x" * 60,
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    await db_session.execute(
        text("UPDATE analyzable_articles SET created_at = :ts WHERE id = :id"),
        {"ts": created_at, "id": article.id},
    )
    await db_session.commit()
    await db_session.refresh(article)
    return article


async def _make_curation(
    db_session: AsyncSession,
    article: AnalyzableArticleRecord,
    *,
    translated_title: str = "tt",
    summary: str = "ss",
) -> ArticleCuration:
    """テスト用 curation を作成する。"""
    curation = ArticleCuration(
        analyzable_article_id=article.id,
        translated_title=translated_title,
        summary=summary,
    )
    db_session.add(curation)
    await db_session.commit()
    await db_session.refresh(curation)
    return curation


async def _make_analyzed_article(
    db_session: AsyncSession,
    curation: ArticleCuration,
    category: Category,
    *,
    embedding: list[float] | None = None,
) -> AnalyzedArticleRecord:
    """テスト用 in-scope assessment を作成する。"""
    assessment = AnalyzedArticleRecord(
        curation_id=curation.id,
        translated_title=curation.translated_title,
        summary=curation.summary,
        investor_take="it",
        category_id=category.id,
        embedding=embedding,
    )
    db_session.add(assessment)
    await db_session.commit()
    await db_session.refresh(assessment)
    return assessment


# analyzable_article_ids_pending_curation


@pytest.mark.asyncio
async def test_pending_curation_returns_articles_without_curation(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """curation 子が無い AnalyzableArticleRecord が境界内なら返る。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/a",
        created_at=now - timedelta(hours=1),
    )

    backlog = PipelineBacklog(db_session)
    ids = await backlog.analyzable_article_ids_pending_curation(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=10,
    )
    assert article.id in ids


@pytest.mark.asyncio
async def test_pending_curation_targets_include_audit_snapshot(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """curation backfill target は analyzable_article_id / source_name を含む。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/curation-target",
        created_at=now - timedelta(hours=1),
    )

    backlog = PipelineBacklog(db_session)
    targets = await backlog.curation_targets_pending(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=10,
    )
    assert any(
        target.target_id == article.id
        and target.analyzable_article_id == article.id
        and target.source_name == str(sample_source.name)
        for target in targets
    )


@pytest.mark.asyncio
async def test_pending_curation_excludes_too_recent(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """pipeline_grace 内 (新しすぎる) は対象外。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/b",
        created_at=now - timedelta(minutes=5),
    )

    backlog = PipelineBacklog(db_session)
    ids = await backlog.analyzable_article_ids_pending_curation(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=10,
    )
    assert article.id not in ids


@pytest.mark.asyncio
async def test_pending_curation_excludes_too_old(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """freshness_window 外 (古すぎる) は対象外。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/c",
        created_at=now - timedelta(days=10),
    )

    backlog = PipelineBacklog(db_session)
    ids = await backlog.analyzable_article_ids_pending_curation(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=10,
    )
    assert article.id not in ids


@pytest.mark.asyncio
async def test_pending_curation_excludes_articles_with_curation(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """curation 子がある AnalyzableArticleRecord は対象外。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/d",
        created_at=now - timedelta(hours=1),
    )
    db_session.add(
        ArticleCuration(
            analyzable_article_id=article.id,
            translated_title="tt",
            summary="ss",
        )
    )
    await db_session.commit()

    backlog = PipelineBacklog(db_session)
    ids = await backlog.analyzable_article_ids_pending_curation(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=10,
    )
    assert article.id not in ids


@pytest.mark.asyncio
async def test_pending_curation_excludes_noise_articles(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """noise 判定済みの article record は再投入対象に入らない。

    signal/noise は排他なので noise 行が在れば curation は完了している。
    旧クエリは ArticleCuration だけ見て noise を child-NULL 扱いしていた
    (latent bug = 無駄な再投入 / 年齢削除ではデータ欠損)。
    """
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/noise",
        created_at=now - timedelta(hours=1),
    )
    db_session.add(
        CurationNoise(
            analyzable_article_id=article.id,
            title_ja="ノイズタイトル",
            summary_ja="ノイズ要約",
        )
    )
    await db_session.commit()

    backlog = PipelineBacklog(db_session)
    ids = await backlog.analyzable_article_ids_pending_curation(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=10,
    )
    assert article.id not in ids


@pytest.mark.asyncio
async def test_count_pending_curation_returns_true_count_without_limit(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """curation backlog COUNT は ID 取得と同じ条件で LIMIT に saturate しない。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    pending = [
        await _make_article(
            db_session,
            sample_source,
            url=f"https://e.com/curation-count-{index}",
            created_at=now - timedelta(hours=1, minutes=index),
        )
        for index in range(3)
    ]
    signal_article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/curation-count-signal",
        created_at=now - timedelta(hours=1),
    )
    await _make_curation(db_session, signal_article)
    noise_article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/curation-count-noise",
        created_at=now - timedelta(hours=1),
    )
    db_session.add(
        CurationNoise(
            analyzable_article_id=noise_article.id,
            title_ja="ノイズタイトル",
            summary_ja="ノイズ要約",
        )
    )
    await db_session.commit()

    backlog = PipelineBacklog(db_session)
    count = await backlog.count_articles_pending_curation(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
    )
    ids = await backlog.analyzable_article_ids_pending_curation(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=2,
    )
    pending_ids = {article.id for article in pending}
    assert count == 3
    assert len(ids) == 2
    assert set(ids).issubset(pending_ids)


# analyzable_article_ids_aged_out_curation (年齢削除対象 = 窓外の child-NULL)


@pytest.mark.asyncio
async def test_aged_out_curation_returns_old_child_null_articles(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """created_before より古い child-NULL AnalyzableArticleRecord が返る。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/aged",
        created_at=now - timedelta(days=10),
    )

    backlog = PipelineBacklog(db_session)
    ids = await backlog.analyzable_article_ids_aged_out_curation(
        created_before=now - timedelta(days=7),
        limit=10,
    )
    assert article.id in ids


@pytest.mark.asyncio
async def test_aged_out_curation_excludes_recent_articles(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """created_before 以降の記事は年齢削除対象外 (通常窓と disjoint)。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/recent",
        created_at=now - timedelta(days=1),
    )

    backlog = PipelineBacklog(db_session)
    ids = await backlog.analyzable_article_ids_aged_out_curation(
        created_before=now - timedelta(days=7),
        limit=10,
    )
    assert article.id not in ids


@pytest.mark.asyncio
async def test_aged_out_curation_excludes_articles_with_curation(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """signal (ArticleCuration) を持つ古い記事は削除対象外。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/aged-signal",
        created_at=now - timedelta(days=10),
    )
    db_session.add(
        ArticleCuration(
            analyzable_article_id=article.id, translated_title="tt", summary="ss"
        )
    )
    await db_session.commit()

    backlog = PipelineBacklog(db_session)
    ids = await backlog.analyzable_article_ids_aged_out_curation(
        created_before=now - timedelta(days=7),
        limit=10,
    )
    assert article.id not in ids


@pytest.mark.asyncio
async def test_aged_out_curation_excludes_articles_with_noise(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """noise を持つ古い記事は削除対象外 (data-loss 防止の要点)。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/aged-noise",
        created_at=now - timedelta(days=10),
    )
    db_session.add(
        CurationNoise(
            analyzable_article_id=article.id,
            title_ja="ノイズタイトル",
            summary_ja="ノイズ要約",
        )
    )
    await db_session.commit()

    backlog = PipelineBacklog(db_session)
    ids = await backlog.analyzable_article_ids_aged_out_curation(
        created_before=now - timedelta(days=7),
        limit=10,
    )
    assert article.id not in ids


# curation_ids_pending_assessment (案 3 で返却列を ArticleCuration.id に変更)


@pytest.mark.asyncio
async def test_pending_assessment_returns_curations_without_analysis_or_rejection(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """curation はあるが analysis / rejection が無い Curation ID が返る。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/cls",
        created_at=now - timedelta(hours=1),
    )
    curation = ArticleCuration(
        analyzable_article_id=article.id,
        translated_title="tt",
        summary="ss",
    )
    db_session.add(curation)
    await db_session.commit()
    await db_session.refresh(curation)

    backlog = PipelineBacklog(db_session)
    ids = await backlog.curation_ids_pending_assessment(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=10,
    )
    assert curation.id in ids


@pytest.mark.asyncio
async def test_pending_assessment_targets_include_audit_snapshot(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """assessment backfill target は curation_id / analyzable_article_id / source_name を含む。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/assessment-target",
        created_at=now - timedelta(hours=1),
    )
    curation = await _make_curation(db_session, article)

    backlog = PipelineBacklog(db_session)
    targets = await backlog.assessment_targets_pending(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=10,
    )
    assert any(
        target.target_id == curation.id
        and target.analyzable_article_id == article.id
        and target.source_name == str(sample_source.name)
        for target in targets
    )


@pytest.mark.asyncio
async def test_pending_assessment_excludes_curations_with_analysis(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """analysis 子があれば assessment は不要なので返らない。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/done",
        created_at=now - timedelta(hours=1),
    )
    curation = ArticleCuration(
        analyzable_article_id=article.id,
        translated_title="tt",
        summary="ss",
    )
    db_session.add(curation)
    await db_session.commit()
    await db_session.refresh(curation)
    db_session.add(
        AnalyzedArticleRecord(
            curation_id=curation.id,
            translated_title="tt",
            summary="ss",
            investor_take="it",
            category_id=sample_categories[0].id,
        )
    )
    await db_session.commit()

    backlog = PipelineBacklog(db_session)
    ids = await backlog.curation_ids_pending_assessment(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=10,
    )
    assert curation.id not in ids


@pytest.mark.asyncio
async def test_pending_assessment_excludes_backfill_excluded_curations(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """soft exclude 済み curation は通常 assessment backfill に出ない。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/assessment-excluded",
        created_at=now - timedelta(hours=1),
    )
    curation = await _make_curation(db_session, article)
    db_session.add(
        AssessmentBackfillExclusion(
            curation_id=curation.id,
            reason_code=BackfillExclusionReason.ASSESSMENT_AGED_OUT.value,
        )
    )
    await db_session.commit()

    backlog = PipelineBacklog(db_session)
    ids = await backlog.curation_ids_pending_assessment(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=10,
    )
    count = await backlog.count_curations_pending_assessment(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
    )
    assert curation.id not in ids
    assert count == 0


# curation_ids_aged_out_assessment


@pytest.mark.asyncio
async def test_aged_out_assessment_returns_old_unassessed_curations(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """7日窓から落ちた未 assessment curation が soft exclude 候補に出る。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/assessment-aged",
        created_at=now - timedelta(days=10),
    )
    curation = await _make_curation(db_session, article)

    backlog = PipelineBacklog(db_session)
    ids = await backlog.curation_ids_aged_out_assessment(
        created_before=now - timedelta(days=7),
        limit=10,
    )
    assert curation.id in ids


@pytest.mark.asyncio
async def test_aged_out_assessment_excludes_recent_completed_and_excluded(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """recent / assessment 済み / exclusion 済みは age-out 候補に出ない。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    recent_article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/assessment-recent",
        created_at=now - timedelta(days=1),
    )
    recent = await _make_curation(db_session, recent_article)

    done_article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/assessment-done-old",
        created_at=now - timedelta(days=10),
    )
    done = await _make_curation(db_session, done_article)
    await _make_analyzed_article(db_session, done, sample_categories[0])

    rejected_article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/assessment-oos-old",
        created_at=now - timedelta(days=10),
    )
    rejected = await _make_curation(db_session, rejected_article)
    db_session.add(
        OutOfScopeArticleRecord(
            curation_id=rejected.id,
            translated_title=rejected.translated_title,
            summary=rejected.summary,
            investor_take="not relevant",
        )
    )

    excluded_article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/assessment-excluded-old",
        created_at=now - timedelta(days=10),
    )
    excluded = await _make_curation(db_session, excluded_article)
    db_session.add(
        AssessmentBackfillExclusion(
            curation_id=excluded.id,
            reason_code=BackfillExclusionReason.ASSESSMENT_AGED_OUT.value,
        )
    )
    await db_session.commit()

    backlog = PipelineBacklog(db_session)
    ids = await backlog.curation_ids_aged_out_assessment(
        created_before=now - timedelta(days=7),
        limit=10,
    )
    assert recent.id not in ids
    assert done.id not in ids
    assert rejected.id not in ids
    assert excluded.id not in ids


@pytest.mark.asyncio
async def test_assessment_backfill_exclusion_reason_code_check(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """DB CHECK が assessment exclusion の不正 reason_code を拒む。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/assessment-bad-reason",
        created_at=now - timedelta(days=10),
    )
    curation = await _make_curation(db_session, article)
    db_session.add(
        AssessmentBackfillExclusion(curation_id=curation.id, reason_code="bad_reason")
    )

    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


# analyzed_article_ids_pending_embedding


@pytest.mark.asyncio
async def test_pending_embedding_returns_analysis_with_null_embedding(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """analysis.embedding IS NULL の Analysis ID が境界内なら返る。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/emb",
        created_at=now - timedelta(hours=1),
    )
    curation = ArticleCuration(
        analyzable_article_id=article.id,
        translated_title="tt",
        summary="ss",
    )
    db_session.add(curation)
    await db_session.commit()
    await db_session.refresh(curation)
    analysis = AnalyzedArticleRecord(
        curation_id=curation.id,
        translated_title="tt",
        summary="ss",
        investor_take="it",
        category_id=sample_categories[0].id,
        # embedding はあえて未指定 → NULL
    )
    db_session.add(analysis)
    await db_session.commit()
    await db_session.refresh(analysis)

    backlog = PipelineBacklog(db_session)
    ids = await backlog.analyzed_article_ids_pending_embedding(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=10,
    )
    assert analysis.id in ids


@pytest.mark.asyncio
async def test_pending_embedding_targets_include_audit_snapshot(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """embedding backfill target は analyzed_article_id と audit snapshot を含む。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/embedding-target",
        created_at=now - timedelta(hours=1),
    )
    curation = await _make_curation(db_session, article)
    analysis = await _make_analyzed_article(
        db_session,
        curation,
        sample_categories[0],
    )

    backlog = PipelineBacklog(db_session)
    targets = await backlog.embedding_targets_pending(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=10,
    )
    assert any(
        target.target_id == analysis.id
        and target.analyzable_article_id == article.id
        and target.source_name == str(sample_source.name)
        for target in targets
    )


@pytest.mark.asyncio
async def test_pending_embedding_excludes_already_embedded(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """embedding が既に書かれていれば対象外。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/embedded",
        created_at=now - timedelta(hours=1),
    )
    curation = ArticleCuration(
        analyzable_article_id=article.id,
        translated_title="tt",
        summary="ss",
    )
    db_session.add(curation)
    await db_session.commit()
    await db_session.refresh(curation)
    analysis = AnalyzedArticleRecord(
        curation_id=curation.id,
        translated_title="tt",
        summary="ss",
        investor_take="it",
        category_id=sample_categories[0].id,
        embedding=[0.1] * 768,
    )
    db_session.add(analysis)
    await db_session.commit()
    await db_session.refresh(analysis)

    backlog = PipelineBacklog(db_session)
    ids = await backlog.analyzed_article_ids_pending_embedding(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=10,
    )
    assert analysis.id not in ids


@pytest.mark.asyncio
async def test_pending_embedding_excludes_backfill_excluded_analysis(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """soft exclude 済み analysis は通常 embedding backfill に出ない。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/embedding-excluded",
        created_at=now - timedelta(hours=1),
    )
    curation = await _make_curation(db_session, article)
    analysis = await _make_analyzed_article(
        db_session,
        curation,
        sample_categories[0],
    )
    db_session.add(
        EmbeddingBackfillExclusion(
            analyzed_article_id=analysis.id,
            reason_code=BackfillExclusionReason.EMBEDDING_AGED_OUT.value,
        )
    )
    await db_session.commit()

    backlog = PipelineBacklog(db_session)
    ids = await backlog.analyzed_article_ids_pending_embedding(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=10,
    )
    assert analysis.id not in ids


@pytest.mark.asyncio
async def test_count_pending_embedding_returns_true_count_without_limit(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """embedding backlog COUNT は ID 取得と同じ条件で LIMIT に saturate しない。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    pending: list[AnalyzedArticleRecord] = []
    for index in range(3):
        article = await _make_article(
            db_session,
            sample_source,
            url=f"https://e.com/embedding-count-{index}",
            created_at=now - timedelta(hours=1, minutes=index),
        )
        pending.append(
            await _make_analyzed_article(
                db_session,
                await _make_curation(db_session, article),
                sample_categories[0],
            )
        )

    embedded_article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/embedding-count-done",
        created_at=now - timedelta(hours=1),
    )
    await _make_analyzed_article(
        db_session,
        await _make_curation(db_session, embedded_article),
        sample_categories[0],
        embedding=[0.1] * 768,
    )

    excluded_article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/embedding-count-excluded",
        created_at=now - timedelta(hours=1),
    )
    excluded = await _make_analyzed_article(
        db_session,
        await _make_curation(db_session, excluded_article),
        sample_categories[0],
    )
    db_session.add(
        EmbeddingBackfillExclusion(
            analyzed_article_id=excluded.id,
            reason_code=BackfillExclusionReason.EMBEDDING_AGED_OUT.value,
        )
    )
    await db_session.commit()

    backlog = PipelineBacklog(db_session)
    count = await backlog.count_analyzed_articles_pending_embedding(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
    )
    ids = await backlog.analyzed_article_ids_pending_embedding(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=2,
    )
    pending_ids = {analysis.id for analysis in pending}
    assert count == 3
    assert len(ids) == 2
    assert set(ids).issubset(pending_ids)


# analyzed_article_ids_aged_out_embedding


@pytest.mark.asyncio
async def test_aged_out_embedding_returns_old_null_embedding_analysis(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """7日窓から落ちた embedding NULL analysis が soft exclude 候補に出る。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/embedding-aged",
        created_at=now - timedelta(days=10),
    )
    curation = await _make_curation(db_session, article)
    analysis = await _make_analyzed_article(
        db_session,
        curation,
        sample_categories[0],
    )

    backlog = PipelineBacklog(db_session)
    ids = await backlog.analyzed_article_ids_aged_out_embedding(
        created_before=now - timedelta(days=7),
        limit=10,
    )
    assert analysis.id in ids


@pytest.mark.asyncio
async def test_aged_out_embedding_excludes_recent_embedded_and_excluded(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """recent / embedding 済み / exclusion 済みは age-out 候補に出ない。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    recent_article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/embedding-recent",
        created_at=now - timedelta(days=1),
    )
    recent = await _make_analyzed_article(
        db_session,
        await _make_curation(db_session, recent_article),
        sample_categories[0],
    )

    embedded_article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/embedding-done-old",
        created_at=now - timedelta(days=10),
    )
    embedded = await _make_analyzed_article(
        db_session,
        await _make_curation(db_session, embedded_article),
        sample_categories[0],
        embedding=[0.1] * 768,
    )

    excluded_article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/embedding-excluded-old",
        created_at=now - timedelta(days=10),
    )
    excluded = await _make_analyzed_article(
        db_session,
        await _make_curation(db_session, excluded_article),
        sample_categories[0],
    )
    db_session.add(
        EmbeddingBackfillExclusion(
            analyzed_article_id=excluded.id,
            reason_code=BackfillExclusionReason.EMBEDDING_AGED_OUT.value,
        )
    )
    await db_session.commit()

    backlog = PipelineBacklog(db_session)
    ids = await backlog.analyzed_article_ids_aged_out_embedding(
        created_before=now - timedelta(days=7),
        limit=10,
    )
    assert recent.id not in ids
    assert embedded.id not in ids
    assert excluded.id not in ids


@pytest.mark.asyncio
async def test_embedding_backfill_exclusion_reason_code_check(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """DB CHECK が embedding exclusion の不正 reason_code を拒む。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/embedding-bad-reason",
        created_at=now - timedelta(days=10),
    )
    curation = await _make_curation(db_session, article)
    analysis = await _make_analyzed_article(
        db_session,
        curation,
        sample_categories[0],
    )
    db_session.add(
        EmbeddingBackfillExclusion(
            analyzed_article_id=analysis.id, reason_code="bad_reason"
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
