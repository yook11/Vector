"""CurationRepository の DB 境界テスト。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.curation.ai.envelope import CurationCall
from app.analysis.curation.domain import Noise, Signal
from app.analysis.curation.repository import CurationRepository
from app.models.article import Article
from app.models.article_curation import ArticleCuration
from app.models.curation_noise import CurationNoise
from app.models.news_source import NewsSource

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _signal_call(
    title_ja: str = "翻訳タイトル",
    summary_ja: str = "要約",
) -> CurationCall[Signal]:
    """``CurationCall[Signal]`` を生成するヘルパー。"""
    return CurationCall(
        result=Signal(title_ja=title_ja, summary_ja=summary_ja),
        raw_response='{"relevance":"signal"}',
        raw_relevance="signal",
        prompt_version="testver1",
        model_name="test-model",
    )


def _noise_call(
    title_ja: str = "ノイズタイトル",
    summary_ja: str = "ノイズ要約",
) -> CurationCall[Noise]:
    """``CurationCall[Noise]`` を生成するヘルパー。"""
    return CurationCall(
        result=Noise(title_ja=title_ja, summary_ja=summary_ja),
        raw_response='{"relevance":"noise"}',
        raw_relevance="noise",
        prompt_version="testver1",
        model_name="test-model",
    )


async def _make_article(
    db_session: AsyncSession, sample_source: NewsSource, url: str
) -> Article:
    article = Article(
        source_id=sample_source.id,
        source_url=url,
        original_title="Title",
        original_content="content body content body",
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    return article


# ===========================================================================
# signal path
# ===========================================================================

# ---------------------------------------------------------------------------
# signal_exists_for_article
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signal_exists_for_article_returns_false_when_no_extraction(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    article = await _make_article(db_session, sample_source, "https://example.com/none")
    repo = CurationRepository(db_session)
    assert await repo.signal_exists_for_article(article.id) is False


@pytest.mark.asyncio
async def test_signal_exists_for_article_returns_true_after_save(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    article = await _make_article(
        db_session, sample_source, "https://example.com/exists"
    )
    repo = CurationRepository(db_session)
    curation_id = await repo.save_signal(_signal_call(), article_id=article.id)
    await db_session.commit()
    assert curation_id is not None
    assert await repo.signal_exists_for_article(article.id) is True


# ---------------------------------------------------------------------------
# save_signal → int | None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_signal_returns_persisted_id(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    article = await _make_article(db_session, sample_source, "https://example.com/save")
    repo = CurationRepository(db_session)
    curation_id = await repo.save_signal(
        _signal_call(title_ja="保存後", summary_ja="要約"),
        article_id=article.id,
    )
    await db_session.commit()

    assert curation_id is not None
    assert curation_id > 0
    # 永続化された行が新規 id と一致する
    persisted = (
        await db_session.execute(
            select(ArticleCuration).where(ArticleCuration.id == curation_id)
        )
    ).scalar_one()
    assert persisted.translated_title == "保存後"
    assert persisted.extracted_at.tzinfo is not None


@pytest.mark.asyncio
async def test_save_signal_returns_none_on_duplicate_in_same_session(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """同一 article_id への 2 度目の save_signal は None を返す (race 敗北の代理)。"""
    article = await _make_article(db_session, sample_source, "https://example.com/dup")
    repo = CurationRepository(db_session)
    first = await repo.save_signal(_signal_call(), article_id=article.id)
    await db_session.commit()
    assert first is not None

    second = await repo.save_signal(_signal_call(), article_id=article.id)
    assert second is None


# ---------------------------------------------------------------------------
# update_signal_idempotent (re-extraction CLI 用)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_signal_idempotent_updates_parent_in_place(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """parent ``ArticleCuration`` は同じ id のまま値だけ差し替わる。

    parent を DELETE しないことで ``in_scope_assessments`` /
    ``out_of_scope_assessments`` / ``article_embeddings`` / ``watchlist_entries``
    への CASCADE 連鎖が起きないことを構造的に保証する。
    """
    article = await _make_article(
        db_session, sample_source, "https://example.com/update-idempotent"
    )
    repo = CurationRepository(db_session)
    first = await repo.save_signal(_signal_call(), article_id=article.id)
    await db_session.commit()
    assert first is not None
    parent_id = first

    updated_id = await repo.update_signal_idempotent(
        _signal_call(title_ja="新タイトル", summary_ja="新要約"),
        article_id=article.id,
    )
    await db_session.commit()

    assert updated_id == parent_id  # parent UPDATE only — id 不変
    parent_after = (
        await db_session.execute(
            select(ArticleCuration).where(ArticleCuration.id == updated_id)
        )
    ).scalar_one()
    assert parent_after.translated_title == "新タイトル"
    assert parent_after.summary == "新要約"


# ---------------------------------------------------------------------------
# 並行 save_signal 統合テスト
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_save_signal_returns_one_persisted_one_none(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """同一 article_id への並行 save_signal は片方が None になる (ON CONFLICT 動作)。"""
    article = await _make_article(db_session, sample_source, "https://example.com/race")

    async def _save_in_new_session() -> int | None:
        async with session_factory() as session:
            repo = CurationRepository(session)
            saved = await repo.save_signal(_signal_call(), article_id=article.id)
            await session.commit()
            return saved

    results = await asyncio.gather(
        _save_in_new_session(),
        _save_in_new_session(),
    )

    assert sum(1 for r in results if r is not None) == 1
    assert sum(1 for r in results if r is None) == 1

    # 永続化された extraction は 1 件のみ
    rows = (
        (
            await db_session.execute(
                select(ArticleCuration).where(ArticleCuration.article_id == article.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(list(rows)) == 1


# ===========================================================================
# noise path
# ===========================================================================

# ---------------------------------------------------------------------------
# save_noise → int | None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_noise_returns_persisted_id(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """noise 記録が title_ja / summary_ja とともに永続化される。"""
    article = await _make_article(db_session, sample_source, "https://example.com/n2")
    repo = CurationRepository(db_session)
    noise_id = await repo.save_noise(_noise_call(), article_id=article.id)
    await db_session.commit()

    assert noise_id is not None
    persisted = (
        await db_session.execute(
            select(CurationNoise).where(CurationNoise.id == noise_id)
        )
    ).scalar_one()
    assert persisted.title_ja == "ノイズタイトル"
    assert persisted.summary_ja == "ノイズ要約"


@pytest.mark.asyncio
async def test_save_noise_returns_none_on_unique_race_loss(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """同一 article への 2 回目 save_noise (UNIQUE 違反) は None を返す。"""
    article = await _make_article(db_session, sample_source, "https://example.com/n4")
    repo = CurationRepository(db_session)

    first = await repo.save_noise(_noise_call(), article_id=article.id)
    await db_session.commit()
    assert first is not None

    second = await repo.save_noise(
        _noise_call(title_ja="別タイトル"),
        article_id=article.id,
    )
    await db_session.commit()
    assert second is None  # race 敗北は None で表現される


# ===========================================================================
# Ready 構築用 DB 事実取得
# ===========================================================================


@pytest.mark.asyncio
async def test_load_ready_build_facts_returns_values_for_unprocessed_article(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    article = await _make_article(
        db_session, sample_source, "https://example.com/load-ok"
    )
    repo = CurationRepository(db_session)

    facts = await repo.load_ready_build_facts(article.id)

    assert facts is not None
    assert facts.article_id == article.id
    assert facts.original_title == article.original_title
    assert facts.original_content == article.original_content
    assert facts.has_signal_curation is False
    assert facts.has_noise_curation is False


@pytest.mark.asyncio
async def test_load_ready_build_facts_returns_none_when_missing(
    db_session: AsyncSession,
) -> None:
    repo = CurationRepository(db_session)
    assert await repo.load_ready_build_facts(article_id=999_999_999) is None


@pytest.mark.asyncio
async def test_load_ready_build_facts_marks_existing_signal(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    article = await _make_article(
        db_session, sample_source, "https://example.com/load-signal"
    )
    repo = CurationRepository(db_session)
    await repo.save_signal(_signal_call(), article_id=article.id)
    await db_session.commit()

    facts = await repo.load_ready_build_facts(article.id)

    assert facts is not None
    assert facts.has_signal_curation is True
    assert facts.has_noise_curation is False


@pytest.mark.asyncio
async def test_load_ready_build_facts_marks_existing_noise(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    article = await _make_article(
        db_session, sample_source, "https://example.com/load-noise"
    )
    repo = CurationRepository(db_session)
    await repo.save_noise(_noise_call(), article_id=article.id)
    await db_session.commit()

    facts = await repo.load_ready_build_facts(article.id)

    assert facts is not None
    assert facts.has_signal_curation is False
    assert facts.has_noise_curation is True
