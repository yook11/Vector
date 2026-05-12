"""PipelineBacklog の DB 統合テスト (年齢ウィンドウ + 子テーブル NULL の検出)。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.maintenance.backlog import PipelineBacklog
from app.models.article import Article
from app.models.article_extraction import ArticleExtraction
from app.models.category import Category
from app.models.in_scope_assessment import InScopeAssessment
from app.models.news_source import NewsSource


async def _make_article(
    db_session: AsyncSession,
    source: NewsSource,
    *,
    url: str,
    created_at: datetime,
) -> Article:
    """指定 created_at の Article を作成 (server_default を後追い UPDATE で上書き)。"""
    article = Article(
        source_id=source.id,
        source_url=url,
        original_title="title",
        original_content="x" * 60,
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    await db_session.execute(
        text("UPDATE articles SET created_at = :ts WHERE id = :id"),
        {"ts": created_at, "id": article.id},
    )
    await db_session.commit()
    await db_session.refresh(article)
    return article


# ---------------------------------------------------------------------------
# article_ids_pending_extraction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_extraction_returns_articles_without_extraction(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """extraction 子が無い Article が境界内なら返る。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/a",
        created_at=now - timedelta(hours=1),
    )

    backlog = PipelineBacklog(db_session)
    ids = await backlog.article_ids_pending_extraction(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=10,
    )
    assert article.id in ids


@pytest.mark.asyncio
async def test_pending_extraction_excludes_too_recent(
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
    ids = await backlog.article_ids_pending_extraction(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=10,
    )
    assert article.id not in ids


@pytest.mark.asyncio
async def test_pending_extraction_excludes_too_old(
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
    ids = await backlog.article_ids_pending_extraction(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=10,
    )
    assert article.id not in ids


@pytest.mark.asyncio
async def test_pending_extraction_excludes_articles_with_extraction(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """extraction 子がある Article は対象外。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/d",
        created_at=now - timedelta(hours=1),
    )
    db_session.add(
        ArticleExtraction(
            article_id=article.id,
            translated_title="tt",
            summary="ss",
            ai_model="m",
        )
    )
    await db_session.commit()

    backlog = PipelineBacklog(db_session)
    ids = await backlog.article_ids_pending_extraction(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=10,
    )
    assert article.id not in ids


# ---------------------------------------------------------------------------
# extraction_ids_pending_assessment (案 3 で返却列を ArticleExtraction.id に変更)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_assessment_returns_extractions_without_analysis_or_rejection(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """extraction はあるが analysis / rejection が無い Extraction ID が返る。"""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/cls",
        created_at=now - timedelta(hours=1),
    )
    extraction = ArticleExtraction(
        article_id=article.id,
        translated_title="tt",
        summary="ss",
        ai_model="m",
    )
    db_session.add(extraction)
    await db_session.commit()
    await db_session.refresh(extraction)

    backlog = PipelineBacklog(db_session)
    ids = await backlog.extraction_ids_pending_assessment(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=10,
    )
    assert extraction.id in ids


@pytest.mark.asyncio
async def test_pending_assessment_excludes_extractions_with_analysis(
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
    extraction = ArticleExtraction(
        article_id=article.id,
        translated_title="tt",
        summary="ss",
        ai_model="m",
    )
    db_session.add(extraction)
    await db_session.commit()
    await db_session.refresh(extraction)
    db_session.add(
        InScopeAssessment(
            extraction_id=extraction.id,
            translated_title="tt",
            summary="ss",
            investor_take="it",
            ai_model="m",
            topic="ai chip",
            category_id=sample_categories[0].id,
        )
    )
    await db_session.commit()

    backlog = PipelineBacklog(db_session)
    ids = await backlog.extraction_ids_pending_assessment(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=10,
    )
    assert extraction.id not in ids


# ---------------------------------------------------------------------------
# analysis_ids_pending_embedding (Phase 2: Article ID → Analysis ID)
# ---------------------------------------------------------------------------


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
    extraction = ArticleExtraction(
        article_id=article.id,
        translated_title="tt",
        summary="ss",
        ai_model="m",
    )
    db_session.add(extraction)
    await db_session.commit()
    await db_session.refresh(extraction)
    analysis = InScopeAssessment(
        extraction_id=extraction.id,
        translated_title="tt",
        summary="ss",
        investor_take="it",
        ai_model="m",
        topic="ai chip",
        category_id=sample_categories[0].id,
        # embedding はあえて未指定 → NULL
    )
    db_session.add(analysis)
    await db_session.commit()
    await db_session.refresh(analysis)

    backlog = PipelineBacklog(db_session)
    ids = await backlog.analysis_ids_pending_embedding(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=10,
    )
    assert analysis.id in ids


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
    extraction = ArticleExtraction(
        article_id=article.id,
        translated_title="tt",
        summary="ss",
        ai_model="m",
    )
    db_session.add(extraction)
    await db_session.commit()
    await db_session.refresh(extraction)
    analysis = InScopeAssessment(
        extraction_id=extraction.id,
        translated_title="tt",
        summary="ss",
        investor_take="it",
        ai_model="m",
        topic="ai chip",
        category_id=sample_categories[0].id,
        embedding=[0.1] * 768,
    )
    db_session.add(analysis)
    await db_session.commit()
    await db_session.refresh(analysis)

    backlog = PipelineBacklog(db_session)
    ids = await backlog.analysis_ids_pending_embedding(
        created_before=now - timedelta(minutes=30),
        created_after=now - timedelta(days=7),
        limit=10,
    )
    assert analysis.id not in ids
