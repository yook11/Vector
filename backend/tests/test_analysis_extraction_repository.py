"""analysis BC の ExtractionRepository 統合テスト (Phase 1B α-1)。

`exists_for_article` / `save` (`Extraction | None` 戻り値) /
race 敗北時の orphan エンティティ非生成 / `find_by_article_id` 復元を検証する。

Phase 1B α-1 で旧 ``article_entities`` から ``article_extraction_entities`` に
clean break。テスト対象も新 ORM (``ArticleExtractionEntity``) と新 schema
(``ExtractedEntity {surface, raw_type}``) に追従。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.analysis.domain.value_objects.entity import (
    EntityRawType,
    EntitySurface,
)
from app.analysis.extraction.ai.envelope import ExtractionCall
from app.analysis.extraction.domain import ExtractedEntity, Signal
from app.analysis.extraction.repository import ExtractionRepository
from app.models.article import Article
from app.models.article_extraction import ArticleExtraction
from app.models.article_extraction_entity import ArticleExtractionEntity
from app.models.news_source import NewsSource


def _signal_call(
    title_ja: str = "翻訳タイトル",
    summary_ja: str = "要約",
    entities: list[tuple[str, str]] | None = None,
) -> ExtractionCall[Signal]:
    """``ExtractionCall[Signal]`` を生成するヘルパー。"""
    if entities is None:
        entities = [("MIT", "company")]
    return ExtractionCall(
        result=Signal(
            title_ja=title_ja,
            summary_ja=summary_ja,
            entities=[
                ExtractedEntity(surface=EntitySurface(s), raw_type=EntityRawType(t))
                for s, t in entities
            ],
        ),
        raw_response='{"relevance":"signal"}',
        raw_relevance="signal",
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


# ---------------------------------------------------------------------------
# exists_for_article
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exists_for_article_returns_false_when_no_extraction(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    article = await _make_article(db_session, sample_source, "https://example.com/none")
    repo = ExtractionRepository(db_session)
    assert await repo.exists_for_article(article.id) is False


@pytest.mark.asyncio
async def test_exists_for_article_returns_true_after_save(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    article = await _make_article(
        db_session, sample_source, "https://example.com/exists"
    )
    repo = ExtractionRepository(db_session)
    saved = await repo.save(_signal_call(), article_id=article.id)
    await db_session.commit()
    assert saved is not None
    assert await repo.exists_for_article(article.id) is True


# ---------------------------------------------------------------------------
# save → Extraction | None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_returns_extraction_with_persisted_id(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    article = await _make_article(db_session, sample_source, "https://example.com/save")
    repo = ExtractionRepository(db_session)
    saved = await repo.save(
        _signal_call(title_ja="保存後", summary_ja="要約"),
        article_id=article.id,
    )
    await db_session.commit()

    assert saved is not None
    assert saved.id > 0
    assert saved.translated_title == "保存後"
    assert saved.extracted_at.tzinfo is not None


@pytest.mark.asyncio
async def test_save_returns_none_on_duplicate_in_same_session(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """同一 article_id への 2 度目の save は None を返す (race 敗北の代理)。"""
    article = await _make_article(db_session, sample_source, "https://example.com/dup")
    repo = ExtractionRepository(db_session)
    first = await repo.save(_signal_call(), article_id=article.id)
    await db_session.commit()
    assert first is not None

    second = await repo.save(_signal_call(), article_id=article.id)
    assert second is None


@pytest.mark.asyncio
async def test_save_does_not_create_orphan_entities_on_race_loss(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """race 敗北 (None 戻り) 時に子テーブル ArticleExtractionEntity が増えないこと。"""
    article = await _make_article(
        db_session, sample_source, "https://example.com/orphan"
    )
    repo = ExtractionRepository(db_session)
    first = await repo.save(
        _signal_call(entities=[("First", "company")]),
        article_id=article.id,
    )
    await db_session.commit()
    assert first is not None

    before = (await db_session.execute(select(ArticleExtractionEntity))).scalars().all()
    before_count = len(list(before))

    second = await repo.save(
        _signal_call(entities=[("Second", "company"), ("Third", "company")]),
        article_id=article.id,
    )
    await db_session.commit()
    assert second is None

    after = (await db_session.execute(select(ArticleExtractionEntity))).scalars().all()
    assert len(list(after)) == before_count


@pytest.mark.asyncio
async def test_save_persists_entities_when_parent_succeeds(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    article = await _make_article(
        db_session, sample_source, "https://example.com/entities"
    )
    repo = ExtractionRepository(db_session)
    saved = await repo.save(
        _signal_call(entities=[("MIT", "company"), ("CRISPR", "technology")]),
        article_id=article.id,
    )
    await db_session.commit()

    assert saved is not None
    rows = (
        (
            await db_session.execute(
                select(ArticleExtractionEntity).where(
                    ArticleExtractionEntity.extraction_id == saved.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(list(rows)) == 2


# ---------------------------------------------------------------------------
# find_by_article_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_by_article_id_returns_none_for_missing(
    db_session: AsyncSession,
) -> None:
    repo = ExtractionRepository(db_session)
    assert await repo.find_by_article_id(999_999) is None


@pytest.mark.asyncio
async def test_find_by_article_id_round_trips_entity(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    article = await _make_article(db_session, sample_source, "https://example.com/find")
    repo = ExtractionRepository(db_session)
    saved = await repo.save(
        _signal_call(entities=[("X", "company")]),
        article_id=article.id,
    )
    await db_session.commit()
    assert saved is not None

    # 別セッションで find して round-trip を検証する (selectinload 経由)
    async with session_factory() as fresh:
        fresh_repo = ExtractionRepository(fresh)
        found = await fresh_repo.find_by_article_id(article.id)
    assert found is not None
    assert found.id == saved.id
    assert found.translated_title == saved.translated_title
    assert tuple(e.surface.root for e in found.entities) == ("X",)


# ---------------------------------------------------------------------------
# update_idempotent (re-extraction CLI 用)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_idempotent_replaces_entities_and_keeps_parent(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """parent ``ArticleExtraction`` は同じ id のまま、child のみ差し替わる。

    parent を DELETE しないことで ``in_scope_assessments`` /
    ``out_of_scope_assessments`` / ``article_embeddings`` / ``watchlist_entries``
    への CASCADE 連鎖が起きないことを構造的に保証する。
    """
    article = await _make_article(
        db_session, sample_source, "https://example.com/update-idempotent"
    )
    repo = ExtractionRepository(db_session)
    first = await repo.save(
        _signal_call(entities=[("OldOne", "company"), ("OldTwo", "person")]),
        article_id=article.id,
    )
    await db_session.commit()
    assert first is not None
    parent_id = first.id

    updated = await repo.update_idempotent(
        _signal_call(
            title_ja="新タイトル",
            summary_ja="新要約",
            entities=[("NewSurface", "Company")],
        ),
        article_id=article.id,
    )
    await db_session.commit()

    assert updated.id == parent_id  # parent UPDATE only
    assert updated.translated_title == "新タイトル"

    rows = (
        (
            await db_session.execute(
                select(ArticleExtractionEntity).where(
                    ArticleExtractionEntity.extraction_id == parent_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert [r.surface.root for r in rows] == ["NewSurface"]
    assert [r.raw_type.root for r in rows] == ["Company"]
    assert [r.position for r in rows] == [0]


# ---------------------------------------------------------------------------
# 並行 save 統合テスト
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_save_returns_one_persisted_one_none(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """同一 article_id への並行 save は片方が None になる (ON CONFLICT 動作)。"""
    article = await _make_article(db_session, sample_source, "https://example.com/race")

    async def _save_in_new_session():
        async with session_factory() as session:
            repo = ExtractionRepository(session)
            saved = await repo.save(_signal_call(), article_id=article.id)
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
                select(ArticleExtraction).where(
                    ArticleExtraction.article_id == article.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(list(rows)) == 1
