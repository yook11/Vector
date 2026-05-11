"""``OutOfScopeRepository`` のユニットテスト。

PR2 で ``save`` signature が ``InScopeRepository.save`` と対称化され、
Stage 3 由来 snapshot (``translated_title`` / ``summary``) を引数経由で受け取る
ようになった。2026-05-11 改修で戻り値が ``int | None`` に縮退 (audit / Stage 5
chain には id だけあれば十分、`feedback_bc_boundary_guarantees_downstream`)。

業務観点の不変条件のみを assert する (memory
``feedback_test_invariants_over_change_tracking``):
- 成功時、戻り値の id が DB 上の主キーと一致し、snapshot が DB row に保持される
- 既存 row 存在時 (race lost) には ``save`` が ``None`` を返し、勝者の row が
  そのまま残る
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.assessment.ai.schema import OutOfScope
from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.out_of_scope_repository import OutOfScopeRepository
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
    """``save`` 成功時、引数 snapshot が DB row に反映され戻り値は新規 id。"""
    extraction = await _make_extraction(db_session, sample_source)
    repo = OutOfScopeRepository(db_session)

    saved_id = await repo.save(
        OutOfScope(investor_take="not relevant"),
        ready=ReadyForAssessment(
            extraction_id=extraction.id,
            translated_title=extraction.translated_title,
            summary=extraction.summary,
        ),
        ai_model=_AI_MODEL,
    )
    await db_session.commit()

    assert isinstance(saved_id, int) and saved_id > 0

    row = (
        await db_session.execute(
            select(OutOfScopeAssessmentORM).where(
                OutOfScopeAssessmentORM.extraction_id == extraction.id
            )
        )
    ).scalar_one()
    assert row.id == saved_id
    assert row.translated_title == extraction.translated_title
    assert row.summary == extraction.summary
    assert row.investor_take == "not relevant"


@pytest.mark.asyncio
async def test_save_returns_none_on_race_lost(
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """既存 row 存在時 ``save`` は ``None`` を返し、勝者 row はそのまま残る。"""
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
    saved_id = await repo.save(
        OutOfScope(investor_take="loser take"),
        ready=ReadyForAssessment(
            extraction_id=extraction.id,
            translated_title="敗者タイトル",
            summary="敗者要約",
        ),
        ai_model=_AI_MODEL,
    )
    await db_session.commit()

    assert saved_id is None
    # 勝者 row は影響を受けない
    row = (
        await db_session.execute(
            select(OutOfScopeAssessmentORM).where(
                OutOfScopeAssessmentORM.extraction_id == extraction.id
            )
        )
    ).scalar_one()
    assert row.translated_title == "勝者タイトル"
    assert row.summary == "勝者要約"
    assert row.investor_take == "winner take"
