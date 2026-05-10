"""``OutOfScopeRepository`` のユニットテスト。

PR2 で ``save`` signature が ``InScopeRepository.save`` と対称化され、
Stage 3 由来 snapshot (``translated_title`` / ``summary``) を引数経由で受け取る
ようになったことを ResponsibilityBoundary レベルで保護する。

業務観点の不変条件のみを assert する (memory
``feedback_test_invariants_over_change_tracking``):
- 成功時の戻り値 Entity と DB row が引数 snapshot を保持していること
- 既存 row 存在時 (race lost) には ``save`` が ``None`` を返し、
  ``find_by_extraction_id`` で勝者を読み戻せること
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.out_of_scope_repository import OutOfScopeRepository
from app.analysis.classifier.schema import OutOfScope
from app.models.article import Article
from app.models.article_extraction import ArticleExtraction
from app.models.news_source import NewsSource
from app.models.out_of_scope_assessment import (
    OutOfScopeAssessment as OutOfScopeAssessmentORM,
)

_AI_MODEL = "gemini-2.5-flash-lite"


async def _make_extraction(
    db_session: AsyncSession, sample_source: NewsSource
) -> ArticleExtraction:
    article = Article(
        source_id=sample_source.id,
        source_url="https://e.com/repo-test",  # type: ignore[arg-type]
        original_title="Original",
        original_content="c" * 100,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    extraction = ArticleExtraction(
        article_id=article.id,
        translated_title="抽出タイトル",
        summary="抽出要約",
        ai_model=_AI_MODEL,
    )
    db_session.add(extraction)
    await db_session.commit()
    await db_session.refresh(extraction)
    return extraction


@pytest.mark.asyncio
async def test_save_persists_snapshot_fields(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """``save`` 成功時、引数 snapshot が Entity 戻り値と DB row の両方に反映される。"""
    extraction = await _make_extraction(db_session, sample_source)
    repo = OutOfScopeRepository(db_session)

    saved = await repo.save(
        OutOfScope(investor_take="not relevant"),
        ready=ReadyForAssessment(
            extraction_id=extraction.id,
            translated_title=extraction.translated_title,
            summary=extraction.summary,
        ),
        ai_model=_AI_MODEL,
    )
    await db_session.commit()

    assert saved is not None
    assert saved.translated_title == extraction.translated_title
    assert saved.summary == extraction.summary

    row = (
        await db_session.execute(
            select(OutOfScopeAssessmentORM).where(
                OutOfScopeAssessmentORM.extraction_id == extraction.id
            )
        )
    ).scalar_one()
    assert row.translated_title == extraction.translated_title
    assert row.summary == extraction.summary


@pytest.mark.asyncio
async def test_save_returns_none_on_race_lost(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """既存 row 存在時 ``save`` は ``None`` を返し、勝者を read-back できる。"""
    extraction = await _make_extraction(db_session, sample_source)
    # 勝者を先に焼く
    winner = OutOfScopeAssessmentORM(
        extraction_id=extraction.id,
        translated_title="勝者タイトル",
        summary="勝者要約",
        investor_take="winner take",
        ai_model=_AI_MODEL,
    )
    db_session.add(winner)
    await db_session.commit()

    repo = OutOfScopeRepository(db_session)
    saved = await repo.save(
        OutOfScope(investor_take="loser take"),
        ready=ReadyForAssessment(
            extraction_id=extraction.id,
            translated_title="敗者タイトル",
            summary="敗者要約",
        ),
        ai_model=_AI_MODEL,
    )
    await db_session.commit()

    assert saved is None
    found = await repo.find_by_extraction_id(extraction.id)
    assert found is not None
    assert found.translated_title == "勝者タイトル"
    assert found.summary == "勝者要約"
    assert found.investor_take == "winner take"
