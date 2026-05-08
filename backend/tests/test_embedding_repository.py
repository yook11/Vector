"""EmbeddingRepository の DB 統合テスト。

is_embedded_for / save (条件付き UPDATE + RETURNING) / find_by_analysis_id /
_to_domain (整合性検査) を実 PostgreSQL に対して検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.embedding.domain.embedding import Embedding, EmbeddingDraft
from app.analysis.embedding.domain.value_objects import EMBEDDING_DIMENSION
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
    embedding_model: str | None = None,
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
        ai_model="gemini-2.5-flash-lite",
    )
    db_session.add(extraction)
    await db_session.flush()
    analysis = InScopeAssessment(
        extraction_id=extraction.id,
        translated_title="分析タイトル",
        summary="分析要約",
        investor_take="投資家視点",
        ai_model="gemini-2.5-flash-lite",
        topic="embedding test",
        category_id=category_id,
        embedding=embedding,
        embedding_model=embedding_model,
    )
    db_session.add(analysis)
    await db_session.commit()
    await db_session.refresh(analysis)
    return analysis


def _zero_vector() -> list[float]:
    return [0.0] * EMBEDDING_DIMENSION


def _draft(value: float = 0.1) -> EmbeddingDraft:
    return EmbeddingDraft.from_inference(vector=[value] * EMBEDDING_DIMENSION)


# ---------------------------------------------------------------------------
# find_by_analysis_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_returns_none_for_missing_analysis(
    db_session: AsyncSession,
) -> None:
    repo = EmbeddingRepository(db_session)
    assert await repo.find_by_analysis_id(999_999) is None


@pytest.mark.asyncio
async def test_find_returns_none_when_embedding_is_null(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    analysis = await _build_analysis(
        db_session,
        sample_source,
        sample_categories[0].id,
        url="https://example.com/no-embedding",
    )

    repo = EmbeddingRepository(db_session)
    assert await repo.find_by_analysis_id(analysis.id) is None


@pytest.mark.asyncio
async def test_find_restores_embedding_entity(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    raw = [0.5] * EMBEDDING_DIMENSION
    analysis = await _build_analysis(
        db_session,
        sample_source,
        sample_categories[0].id,
        url="https://example.com/with-embedding",
        embedding=raw,
        embedding_model="cl-nagoya/ruri-v3-310m",
    )

    repo = EmbeddingRepository(db_session)
    embedding = await repo.find_by_analysis_id(analysis.id)

    assert isinstance(embedding, Embedding)
    assert embedding.analysis_id == analysis.id
    assert embedding.model_name == "cl-nagoya/ruri-v3-310m"
    assert embedding.vector.to_list() == pytest.approx(raw, rel=1e-2)


# ---------------------------------------------------------------------------
# is_embedded_for — cheap exists 判定 (try_advance_from 用)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_embedded_for_returns_false_when_embedding_null(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    analysis = await _build_analysis(
        db_session,
        sample_source,
        sample_categories[0].id,
        url="https://example.com/exists-null",
    )
    repo = EmbeddingRepository(db_session)
    assert await repo.is_embedded_for(analysis.id) is False


@pytest.mark.asyncio
async def test_is_embedded_for_returns_true_when_embedding_present(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    analysis = await _build_analysis(
        db_session,
        sample_source,
        sample_categories[0].id,
        url="https://example.com/exists-yes",
        embedding=_zero_vector(),
        embedding_model="cl-nagoya/ruri-v3-310m",
    )
    repo = EmbeddingRepository(db_session)
    assert await repo.is_embedded_for(analysis.id) is True


@pytest.mark.asyncio
async def test_is_embedded_for_returns_false_for_missing_analysis(
    db_session: AsyncSession,
) -> None:
    repo = EmbeddingRepository(db_session)
    assert await repo.is_embedded_for(999_999) is False


# ---------------------------------------------------------------------------
# save (条件付き UPDATE + RETURNING → Entity | None)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_writes_embedding_and_returns_entity(
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
        _draft(0.3),
        analysis_id=analysis_id,
        model_name="cl-nagoya/ruri-v3-310m",
    )
    await db_session.commit()

    assert isinstance(saved, Embedding)
    assert saved.analysis_id == analysis_id
    assert saved.model_name == "cl-nagoya/ruri-v3-310m"
    db_session.expire_all()
    refetched = await db_session.get(InScopeAssessment, analysis_id)
    assert refetched is not None
    assert refetched.embedding is not None
    assert refetched.embedding_model == "cl-nagoya/ruri-v3-310m"


@pytest.mark.asyncio
async def test_save_returns_none_on_concurrent_write(
    db_session: AsyncSession,
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """既に他ワーカーが書いた行への 2 度目の save は None を返す。"""
    analysis = await _build_analysis(
        db_session,
        sample_source,
        sample_categories[0].id,
        url="https://example.com/already-saved",
        embedding=_zero_vector(),
        embedding_model="cl-nagoya/ruri-v3-310m",
    )
    analysis_id = analysis.id

    repo = EmbeddingRepository(db_session)
    saved = await repo.save(
        _draft(0.7),
        analysis_id=analysis_id,
        model_name="cl-nagoya/ruri-v3-310m",
    )

    assert saved is None
    db_session.expire_all()
    refetched = await db_session.get(InScopeAssessment, analysis_id)
    assert refetched is not None
    # 既存の embedding を上書きしないこと
    assert refetched.embedding is not None
    raw = refetched.embedding
    values = raw.to_list() if hasattr(raw, "to_list") else list(raw)
    assert values[0] == pytest.approx(0.0, abs=1e-3)


@pytest.mark.asyncio
async def test_save_returns_none_for_unknown_analysis_id(
    db_session: AsyncSession,
) -> None:
    repo = EmbeddingRepository(db_session)
    saved = await repo.save(
        _draft(),
        analysis_id=999_999,
        model_name="cl-nagoya/ruri-v3-310m",
    )
    assert saved is None


# ---------------------------------------------------------------------------
# _to_domain — defense-in-depth (片方 NULL 検知)
# ---------------------------------------------------------------------------


def test_to_domain_returns_none_when_both_columns_null() -> None:
    orm = InScopeAssessment(
        id=1,
        extraction_id=1,
        translated_title="t",
        summary="s",
        investor_take="i",
        ai_model="m",
        topic="topic",
        category_id=1,
        embedding=None,
        embedding_model=None,
    )
    assert EmbeddingRepository._to_domain(orm) is None


def test_to_domain_raises_when_embedding_present_but_model_missing() -> None:
    orm = InScopeAssessment(
        id=1,
        extraction_id=1,
        translated_title="t",
        summary="s",
        investor_take="i",
        ai_model="m",
        topic="topic",
        category_id=1,
        embedding=_zero_vector(),
        embedding_model=None,
    )
    with pytest.raises(ValueError, match="inconsistent embedding state"):
        EmbeddingRepository._to_domain(orm)


def test_to_domain_raises_when_model_present_but_embedding_missing() -> None:
    orm = InScopeAssessment(
        id=1,
        extraction_id=1,
        translated_title="t",
        summary="s",
        investor_take="i",
        ai_model="m",
        topic="topic",
        category_id=1,
        embedding=None,
        embedding_model="cl-nagoya/ruri-v3-310m",
    )
    with pytest.raises(ValueError, match="inconsistent embedding state"):
        EmbeddingRepository._to_domain(orm)
