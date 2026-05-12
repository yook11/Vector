"""ExtractionRepository (signal / noise 両 path) の統合テスト。

PR1-b で Stage 3 永続化層を 1 クラスに集約 (旧 ``NoiseRepository`` を吸収)。
PR1-c で戻り値を ``int | None`` に縮小し ``find_*_by_article_id`` 経路を撤去
(race 敗北は audit / chain を焼かない短絡で表現、Stage 4 と完全対称)。

検証する振る舞い:

signal path:
- ``signal_exists_for_article`` の cheap 判定が article_id 単位で正しい
- ``save_signal`` の戻り値 (``int | None``、新規 id / race 敗北 None)
- race 敗北時に orphan ``article_extraction_entities`` 行を作らない
- ``update_signal_idempotent`` で parent UPDATE のみ / child は差し替え
  (戻り値は parent ``int`` で id は不変)

noise path:
- ``noise_exists_for_article`` の cheap 判定が article_id 単位で正しい
- ``save_noise`` で entities が JSONB として position 順で永続化される
- ``save_noise`` の戻り値 (``int | None``、新規 id / race 敗北 None)
- ``save_noise`` の race 敗北 (UNIQUE 違反) 時は ``None`` を返す
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
from app.analysis.extraction.domain import ExtractedEntity, Noise, Signal
from app.analysis.extraction.repository import ExtractionRepository
from app.models.article import Article
from app.models.article_extraction import ArticleExtraction
from app.models.article_extraction_entity import ArticleExtractionEntity
from app.models.extraction_noise import ExtractionNoise as ExtractionNoiseORM
from app.models.news_source import NewsSource

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


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


def _noise_call(
    title_ja: str = "ノイズタイトル",
    summary_ja: str = "ノイズ要約",
    entities: list[tuple[str, str]] | None = None,
) -> ExtractionCall[Noise]:
    """``ExtractionCall[Noise]`` を生成するヘルパー。"""
    if entities is None:
        entities = [("Celebrity X", "person"), ("Local Event", "event")]
    return ExtractionCall(
        result=Noise(
            title_ja=title_ja,
            summary_ja=summary_ja,
            entities=[
                ExtractedEntity(surface=EntitySurface(s), raw_type=EntityRawType(t))
                for s, t in entities
            ],
        ),
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
    repo = ExtractionRepository(db_session)
    assert await repo.signal_exists_for_article(article.id) is False


@pytest.mark.asyncio
async def test_signal_exists_for_article_returns_true_after_save(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    article = await _make_article(
        db_session, sample_source, "https://example.com/exists"
    )
    repo = ExtractionRepository(db_session)
    extraction_id = await repo.save_signal(_signal_call(), article_id=article.id)
    await db_session.commit()
    assert extraction_id is not None
    assert await repo.signal_exists_for_article(article.id) is True


# ---------------------------------------------------------------------------
# save_signal → int | None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_signal_returns_persisted_id(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    article = await _make_article(db_session, sample_source, "https://example.com/save")
    repo = ExtractionRepository(db_session)
    extraction_id = await repo.save_signal(
        _signal_call(title_ja="保存後", summary_ja="要約"),
        article_id=article.id,
    )
    await db_session.commit()

    assert extraction_id is not None
    assert extraction_id > 0
    # 永続化された行が新規 id と一致する
    persisted = (
        await db_session.execute(
            select(ArticleExtraction).where(ArticleExtraction.id == extraction_id)
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
    repo = ExtractionRepository(db_session)
    first = await repo.save_signal(_signal_call(), article_id=article.id)
    await db_session.commit()
    assert first is not None

    second = await repo.save_signal(_signal_call(), article_id=article.id)
    assert second is None


@pytest.mark.asyncio
async def test_save_signal_does_not_create_orphan_entities_on_race_loss(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """race 敗北 (None 戻り) 時に子テーブル ArticleExtractionEntity が増えないこと。"""
    article = await _make_article(
        db_session, sample_source, "https://example.com/orphan"
    )
    repo = ExtractionRepository(db_session)
    first = await repo.save_signal(
        _signal_call(entities=[("First", "company")]),
        article_id=article.id,
    )
    await db_session.commit()
    assert first is not None

    before = (await db_session.execute(select(ArticleExtractionEntity))).scalars().all()
    before_count = len(list(before))

    second = await repo.save_signal(
        _signal_call(entities=[("Second", "company"), ("Third", "company")]),
        article_id=article.id,
    )
    await db_session.commit()
    assert second is None

    after = (await db_session.execute(select(ArticleExtractionEntity))).scalars().all()
    assert len(list(after)) == before_count


@pytest.mark.asyncio
async def test_save_signal_persists_entities_when_parent_succeeds(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    article = await _make_article(
        db_session, sample_source, "https://example.com/entities"
    )
    repo = ExtractionRepository(db_session)
    extraction_id = await repo.save_signal(
        _signal_call(entities=[("MIT", "company"), ("CRISPR", "technology")]),
        article_id=article.id,
    )
    await db_session.commit()

    assert extraction_id is not None
    rows = (
        (
            await db_session.execute(
                select(ArticleExtractionEntity).where(
                    ArticleExtractionEntity.extraction_id == extraction_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(list(rows)) == 2


# ---------------------------------------------------------------------------
# update_signal_idempotent (re-extraction CLI 用)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_signal_idempotent_replaces_entities_and_keeps_parent(
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
    first = await repo.save_signal(
        _signal_call(entities=[("OldOne", "company"), ("OldTwo", "person")]),
        article_id=article.id,
    )
    await db_session.commit()
    assert first is not None
    parent_id = first

    updated_id = await repo.update_signal_idempotent(
        _signal_call(
            title_ja="新タイトル",
            summary_ja="新要約",
            entities=[("NewSurface", "Company")],
        ),
        article_id=article.id,
    )
    await db_session.commit()

    assert updated_id == parent_id  # parent UPDATE only — id 不変
    parent_after = (
        await db_session.execute(
            select(ArticleExtraction).where(ArticleExtraction.id == updated_id)
        )
    ).scalar_one()
    assert parent_after.translated_title == "新タイトル"

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
            repo = ExtractionRepository(session)
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
                select(ArticleExtraction).where(
                    ArticleExtraction.article_id == article.id
                )
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
# noise_exists_for_article
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_noise_exists_for_article_returns_false_when_no_noise(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    article = await _make_article(db_session, sample_source, "https://example.com/n0")
    repo = ExtractionRepository(db_session)
    assert await repo.noise_exists_for_article(article.id) is False


@pytest.mark.asyncio
async def test_noise_exists_for_article_returns_true_after_save(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    article = await _make_article(db_session, sample_source, "https://example.com/n1")
    repo = ExtractionRepository(db_session)
    noise_id = await repo.save_noise(_noise_call(), article_id=article.id)
    await db_session.commit()
    assert noise_id is not None
    assert await repo.noise_exists_for_article(article.id) is True


# ---------------------------------------------------------------------------
# save_noise → int | None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_noise_persists_entities_as_jsonb_in_order(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """JSONB の配列順序が AI 出力順を保持する。"""
    article = await _make_article(db_session, sample_source, "https://example.com/n2")
    repo = ExtractionRepository(db_session)
    noise_id = await repo.save_noise(
        _noise_call(
            entities=[("First", "company"), ("Second", "person"), ("Third", "tech")]
        ),
        article_id=article.id,
    )
    await db_session.commit()

    assert noise_id is not None
    persisted = (
        await db_session.execute(
            select(ExtractionNoiseORM).where(ExtractionNoiseORM.id == noise_id)
        )
    ).scalar_one()
    surfaces = tuple(e["surface"] for e in persisted.entities)
    raw_types = tuple(e["raw_type"] for e in persisted.entities)
    assert surfaces == ("First", "Second", "Third")
    assert raw_types == ("company", "person", "tech")
    assert persisted.title_ja == "ノイズタイトル"


@pytest.mark.asyncio
async def test_save_noise_accepts_empty_entities(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """entities が空でも noise 記録は永続化できる (空配列 JSONB)。"""
    article = await _make_article(db_session, sample_source, "https://example.com/n3")
    repo = ExtractionRepository(db_session)
    noise_id = await repo.save_noise(
        _noise_call(entities=[]),
        article_id=article.id,
    )
    await db_session.commit()

    assert noise_id is not None
    persisted = (
        await db_session.execute(
            select(ExtractionNoiseORM).where(ExtractionNoiseORM.id == noise_id)
        )
    ).scalar_one()
    assert persisted.entities == []


@pytest.mark.asyncio
async def test_save_noise_returns_none_on_unique_race_loss(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """同一 article への 2 回目 save_noise (UNIQUE 違反) は None を返す。"""
    article = await _make_article(db_session, sample_source, "https://example.com/n4")
    repo = ExtractionRepository(db_session)

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
# try_load_for_extraction (PR3 案 3: atomic loader)
# ===========================================================================


@pytest.mark.asyncio
async def test_try_load_returns_ready_when_precondition_met(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """signal/noise 未生成 + 本文サイズ妥当なら厚い Ready を返す。"""
    article = await _make_article(
        db_session, sample_source, "https://example.com/load-ok"
    )
    repo = ExtractionRepository(db_session)

    ready = await repo.try_load_for_extraction(article.id)

    assert ready is not None
    assert ready.article_id == article.id
    assert ready.original_title == article.original_title
    assert ready.original_content == article.original_content


@pytest.mark.asyncio
async def test_try_load_returns_none_when_article_missing(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """Article 不在 (既消滅 / 未永続化) なら None を返す。"""
    repo = ExtractionRepository(db_session)
    assert await repo.try_load_for_extraction(article_id=999_999_999) is None


@pytest.mark.asyncio
async def test_try_load_returns_none_when_signal_exists(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """既に signal extraction が永続化済なら None を返す (再処理しない)。"""
    article = await _make_article(
        db_session, sample_source, "https://example.com/load-signal"
    )
    repo = ExtractionRepository(db_session)
    await repo.save_signal(_signal_call(), article_id=article.id)
    await db_session.commit()

    assert await repo.try_load_for_extraction(article.id) is None


@pytest.mark.asyncio
async def test_try_load_returns_none_when_noise_exists(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """既に noise 判定済なら None (Stage 1 noise 記事を再処理しない)。"""
    article = await _make_article(
        db_session, sample_source, "https://example.com/load-noise"
    )
    repo = ExtractionRepository(db_session)
    await repo.save_noise(_noise_call(), article_id=article.id)
    await db_session.commit()

    assert await repo.try_load_for_extraction(article.id) is None


@pytest.mark.asyncio
async def test_try_load_returns_none_for_oversized_content(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """本文サイズ > MAX_CONTENT_LENGTH なら skip log + None を返す。

    AI 呼び出し前の枝刈り (Stage 4/5 と同じレイヤで実施)。
    """
    from app.analysis.extraction.domain.ready import ReadyForExtraction

    oversized = "x" * (ReadyForExtraction.MAX_CONTENT_LENGTH + 1)
    article = Article(
        source_id=sample_source.id,
        source_url="https://example.com/oversized",
        original_title="Title",
        original_content=oversized,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    repo = ExtractionRepository(db_session)
    assert await repo.try_load_for_extraction(article.id) is None
