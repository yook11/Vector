"""EmbeddingRepository の DB 統合テスト。

try_load_for_embedding / save (条件付き UPDATE + RETURNING) を実 PostgreSQL に
対して検証する。読み戻し / ORM→Entity 復元は廃止済み (2026-05-12)。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.domain.value_objects import (
    EMBEDDING_DIMENSION,
    EmbeddingVector,
)
from app.analysis.embedding.repository import EmbeddingRepository
from app.models.article import Article
from app.models.article_extraction import ArticleExtraction
from app.models.category import Category
from app.models.in_scope_assessment import InScopeAssessment
from app.models.news_source import NewsSource


async def _build_analysis(
    db_session: AsyncSession,
    source: NewsSource,
    category_id: int,
    *,
    url: str,
    embedding: list[float] | None = None,
) -> InScopeAssessment:
    """Stage 2 完了済みの分析行を 1 件作成する。"""
    article = Article(
        source_id=source.id,
        source_url=url,
        original_title="seed",
        original_content="content body content body content body content body",
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.flush()
    extraction = ArticleExtraction(
        article_id=article.id,
        translated_title="抽出タイトル",
        summary="抽出要約",
    )
    db_session.add(extraction)
    await db_session.flush()
    analysis = InScopeAssessment(
        extraction_id=extraction.id,
        translated_title="分析タイトル",
        summary="分析要約",
        investor_take="投資家視点",
        topic="embedding test",
        category_id=category_id,
        embedding=embedding,
    )
    db_session.add(analysis)
    await db_session.commit()
    await db_session.refresh(analysis)
    return analysis


def _zero_vector() -> list[float]:
    return [0.0] * EMBEDDING_DIMENSION


def _vector(value: float = 0.1) -> EmbeddingVector:
    return EmbeddingVector(root=tuple([value] * EMBEDDING_DIMENSION))


# ---------------------------------------------------------------------------
# try_load_for_embedding — atomic 1-query Ready loader (try_advance_from delegate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_try_load_returns_ready_when_embedding_null(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """embedding NULL かつ行存在 → translated_title + summary を結合した Ready。"""
    analysis = await _build_analysis(
        db_session,
        sample_source,
        sample_categories[0].id,
        url="https://example.com/load-null",
    )
    repo = EmbeddingRepository(db_session)
    ready = await repo.try_load_for_embedding(analysis.id)

    assert isinstance(ready, ReadyForEmbedding)
    assert ready.analysis_id == analysis.id
    assert ready.text_for_embedding == "分析タイトル\n分析要約"
    # article_id は ArticleExtraction 1-hop JOIN で取得
    assert ready.article_id > 0


@pytest.mark.asyncio
async def test_try_load_returns_none_when_already_embedded(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """embedding 値あり → None (既 embedded のため再実行不要、業務正常)。"""
    analysis = await _build_analysis(
        db_session,
        sample_source,
        sample_categories[0].id,
        url="https://example.com/load-existing",
        embedding=_zero_vector(),
    )
    repo = EmbeddingRepository(db_session)
    assert await repo.try_load_for_embedding(analysis.id) is None


@pytest.mark.asyncio
async def test_try_load_returns_none_for_missing_analysis(
    db_session: AsyncSession,
) -> None:
    repo = EmbeddingRepository(db_session)
    assert await repo.try_load_for_embedding(999_999) is None


# ---------------------------------------------------------------------------
# save (条件付き UPDATE + RETURNING → bool)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_writes_embedding_and_returns_true(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    analysis = await _build_analysis(
        db_session,
        sample_source,
        sample_categories[0].id,
        url="https://example.com/save",
    )
    analysis_id = analysis.id

    repo = EmbeddingRepository(db_session)
    saved = await repo.save(
        _vector(0.3),
        analysis_id=analysis_id,
    )
    await db_session.commit()

    assert saved is True
    db_session.expire_all()
    refetched = await db_session.get(InScopeAssessment, analysis_id)
    assert refetched is not None
    assert refetched.embedding is not None


@pytest.mark.asyncio
async def test_save_returns_false_on_concurrent_write(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """既に他ワーカーが書いた行への 2 度目の save は False を返す。"""
    analysis = await _build_analysis(
        db_session,
        sample_source,
        sample_categories[0].id,
        url="https://example.com/already-saved",
        embedding=_zero_vector(),
    )
    analysis_id = analysis.id

    repo = EmbeddingRepository(db_session)
    saved = await repo.save(
        _vector(0.7),
        analysis_id=analysis_id,
    )

    assert saved is False
    db_session.expire_all()
    refetched = await db_session.get(InScopeAssessment, analysis_id)
    assert refetched is not None
    # 既存の embedding を上書きしないこと
    assert refetched.embedding is not None
    raw = refetched.embedding
    values = raw.to_list() if hasattr(raw, "to_list") else list(raw)
    assert values[0] == pytest.approx(0.0, abs=1e-3)


@pytest.mark.asyncio
async def test_save_returns_false_for_unknown_analysis_id(
    db_session: AsyncSession,
) -> None:
    repo = EmbeddingRepository(db_session)
    saved = await repo.save(
        _vector(),
        analysis_id=999_999,
    )
    assert saved is False
