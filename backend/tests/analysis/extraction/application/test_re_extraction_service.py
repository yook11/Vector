"""ReExtractionService 統合テスト (Phase 1B α-1)。

検証する観点:

- 新規 article_id (Article 不在) → ``skipped_ids``
- ArticleExtraction 不在 (Article のみ) → ``skipped_ids``
- 正常: 既存 extraction が UPDATE され、子 entity が差し替わる → ``success_ids``
- dry_run=True: extractor は呼ばれるが DB は変更されない (rollback)
- ``InvalidInputError`` → ``skipped_ids`` (failed には入らない)
- ``ProviderError`` を retry 上限まで → ``failed_ids``
- ``ProviderError`` 1 回 → ``ProviderError`` 1 回 → 成功 → ``success_ids``
  (2 回まで retry すれば成功するパターン)
- 親 ``ArticleExtraction.id`` は保持される (CASCADE 連鎖防止の構造保証)
- 再抽出で Noise が返った場合は ``skipped_ids`` に分類され、既存
  ``ArticleExtraction`` は上書きされない (データ破壊防止の構造保証)

extractor は ``unittest.mock`` で差し替え (実 Gemini を呼ばない)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.analysis.domain.value_objects.entity import EntityRawType, EntitySurface
from app.analysis.errors import InvalidInputError, ProviderError
from app.analysis.extraction.ai.base import BaseExtractor
from app.analysis.extraction.ai.envelope import ExtractionCall
from app.analysis.extraction.application import (
    ReExtractionService,
    ReExtractionSummary,
)
from app.analysis.extraction.domain import (
    ExtractedEntity,
    Noise,
    Signal,
)
from app.analysis.extraction.repository import ExtractionRepository
from app.models.article import Article
from app.models.article_extraction import ArticleExtraction
from app.models.article_extraction_entity import ArticleExtractionEntity
from app.models.news_source import NewsSource


def _signal_call(
    entities: list[tuple[str, str]] | None = None,
    *,
    title_ja: str = "新タイトル",
    summary_ja: str = "新要約",
) -> ExtractionCall[Signal]:
    """``ExtractionCall[Signal]`` を生成するヘルパー。"""
    if entities is None:
        entities = [("NewSurface", "Company")]
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
        model_name="test-model-x",
    )


def _noise_call(
    entities: list[tuple[str, str]] | None = None,
    *,
    title_ja: str = "ノイズタイトル",
    summary_ja: str = "ノイズ要約",
) -> ExtractionCall[Noise]:
    """``ExtractionCall[Noise]`` を生成するヘルパー (再抽出で Noise 経路を作る用)。"""
    if entities is None:
        entities = [("NoiseSurface", "Person")]
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
        model_name="test-model-x",
    )


def _extractor(
    *,
    return_value: ExtractionCall[Signal] | ExtractionCall[Noise] | None = None,
    side_effect=None,
) -> BaseExtractor:
    """``BaseExtractor`` の最小モック (model_name + extract のみ)。"""
    mock = MagicMock(spec=BaseExtractor)
    type(mock).model_name = "test-model-x"
    if side_effect is not None:
        mock.extract = AsyncMock(side_effect=side_effect)
    else:
        mock.extract = AsyncMock(return_value=return_value or _signal_call())
    return mock


async def _make_article(
    db_session: AsyncSession, sample_source: NewsSource, url: str
) -> Article:
    article = Article(
        source_id=sample_source.id,
        source_url=url,
        original_title="Original Title",
        original_content="content body content body",
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    return article


async def _seed_extraction(
    db_session: AsyncSession,
    *,
    article: Article,
    entities: list[tuple[str, str]],
) -> ArticleExtraction:
    """Article + 既存 ArticleExtraction (子付き) を作る。"""
    repo = ExtractionRepository(db_session)
    saved = await repo.save(
        _signal_call(
            entities=entities,
            title_ja="旧タイトル",
            summary_ja="旧要約",
        ),
        article_id=article.id,
    )
    await db_session.commit()
    assert saved is not None
    parent = (
        await db_session.execute(
            select(ArticleExtraction).where(ArticleExtraction.article_id == article.id)
        )
    ).scalar_one()
    return parent


# ---------------------------------------------------------------------------
# skip 経路
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skips_when_article_does_not_exist(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = ReExtractionService(session_factory)
    summary = await service.execute((999_999,), _extractor(), dry_run=False)
    assert summary.skipped_ids == (999_999,)
    assert summary.success_ids == ()
    assert summary.failed_ids == ()


@pytest.mark.asyncio
async def test_skips_when_extraction_does_not_exist(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    article = await _make_article(
        db_session, sample_source, "https://example.com/no-extraction"
    )
    service = ReExtractionService(session_factory)
    summary = await service.execute((article.id,), _extractor(), dry_run=False)
    assert summary.skipped_ids == (article.id,)


@pytest.mark.asyncio
async def test_invalid_input_is_skipped_not_failed(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    article = await _make_article(
        db_session, sample_source, "https://example.com/invalid"
    )
    await _seed_extraction(db_session, article=article, entities=[("Old", "company")])

    extractor = _extractor(side_effect=InvalidInputError("too short"))
    service = ReExtractionService(session_factory)
    summary = await service.execute((article.id,), extractor, dry_run=False)

    assert summary.skipped_ids == (article.id,)
    assert summary.failed_ids == ()


# ---------------------------------------------------------------------------
# 正常系
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_replaces_entities_and_keeps_parent_id(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """re-extraction 成功時: parent id は変わらず、子 entity が差し替わる。"""
    article = await _make_article(
        db_session, sample_source, "https://example.com/success"
    )
    parent = await _seed_extraction(
        db_session, article=article, entities=[("OldOne", "company")]
    )
    parent_id_before = parent.id

    extractor = _extractor(
        return_value=_signal_call(entities=[("NewSurface", "Company")])
    )
    service = ReExtractionService(session_factory)
    summary = await service.execute((article.id,), extractor, dry_run=False)

    assert summary.success_ids == (article.id,)
    assert summary.dry_run is False

    async with session_factory() as fresh:
        parent_after = (
            await fresh.execute(
                select(ArticleExtraction).where(
                    ArticleExtraction.article_id == article.id
                )
            )
        ).scalar_one()
        assert parent_after.id == parent_id_before
        assert parent_after.translated_title == "新タイトル"

        rows = (
            (
                await fresh.execute(
                    select(ArticleExtractionEntity).where(
                        ArticleExtractionEntity.extraction_id == parent_id_before
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [r.surface.root for r in rows] == ["NewSurface"]
    assert [r.raw_type.root for r in rows] == ["Company"]


@pytest.mark.asyncio
async def test_dry_run_calls_extractor_but_rolls_back(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """dry_run=True: extractor は呼ばれるが DB は変更されない。"""
    article = await _make_article(db_session, sample_source, "https://example.com/dry")
    await _seed_extraction(
        db_session, article=article, entities=[("OldOne", "company")]
    )

    extractor = _extractor(
        return_value=_signal_call(entities=[("ShouldNotPersist", "Tech")])
    )
    service = ReExtractionService(session_factory)
    summary = await service.execute((article.id,), extractor, dry_run=True)

    assert summary.success_ids == (article.id,)
    assert summary.dry_run is True
    extractor.extract.assert_awaited_once()

    async with session_factory() as fresh:
        parent = (
            await fresh.execute(
                select(ArticleExtraction).where(
                    ArticleExtraction.article_id == article.id
                )
            )
        ).scalar_one()
        # UPDATE が roll back されたので旧タイトルのまま
        assert parent.translated_title == "旧タイトル"

        rows = (
            (
                await fresh.execute(
                    select(ArticleExtractionEntity).where(
                        ArticleExtractionEntity.extraction_id == parent.id
                    )
                )
            )
            .scalars()
            .all()
        )
    # 旧 entity ("OldOne") のまま、新 ("ShouldNotPersist") は永続化されていない
    assert [r.surface.root for r in rows] == ["OldOne"]


# ---------------------------------------------------------------------------
# retry / failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retries_then_succeeds(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """ProviderError 1 回 → 成功で success_ids に入る。"""
    article = await _make_article(
        db_session, sample_source, "https://example.com/retry"
    )
    await _seed_extraction(db_session, article=article, entities=[("Old", "company")])

    extractor = _extractor(side_effect=[ProviderError("transient"), _signal_call()])
    service = ReExtractionService(session_factory, max_retries=3)
    summary = await service.execute((article.id,), extractor, dry_run=False)

    assert summary.success_ids == (article.id,)
    assert extractor.extract.await_count == 2


@pytest.mark.asyncio
async def test_failed_after_max_retries(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """ProviderError が max_retries 回連続で failed_ids に入る。"""
    article = await _make_article(
        db_session, sample_source, "https://example.com/failed"
    )
    await _seed_extraction(db_session, article=article, entities=[("Old", "company")])

    extractor = _extractor(side_effect=ProviderError("dead"))
    service = ReExtractionService(session_factory, max_retries=2)
    summary = await service.execute((article.id,), extractor, dry_run=False)

    assert summary.failed_ids == (article.id,)
    assert summary.success_ids == ()
    assert extractor.extract.await_count == 2


@pytest.mark.asyncio
async def test_summary_aggregates_per_article_independently(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """1 件 success / 1 件 skip (no extraction) / 1 件 failed が独立に集約される。"""
    a_ok = await _make_article(db_session, sample_source, "https://example.com/ok")
    await _seed_extraction(db_session, article=a_ok, entities=[("X", "company")])
    a_skip = await _make_article(db_session, sample_source, "https://example.com/skip")
    a_fail = await _make_article(db_session, sample_source, "https://example.com/fail")
    await _seed_extraction(db_session, article=a_fail, entities=[("Y", "company")])

    call_log: list[int] = []

    async def _extract_side_effect(
        *, title: str, content: str
    ) -> ExtractionCall[Signal] | ExtractionCall[Noise]:
        call_log.append(len(call_log))
        # 順序: a_ok → a_fail (a_skip は extract まで来ない)
        if len(call_log) == 1:
            return _signal_call()
        raise ProviderError("dead")

    extractor = MagicMock(spec=BaseExtractor)
    type(extractor).model_name = "test-model-x"
    extractor.extract = AsyncMock(side_effect=_extract_side_effect)

    service = ReExtractionService(session_factory, max_retries=1)
    summary = await service.execute(
        (a_ok.id, a_skip.id, a_fail.id), extractor, dry_run=False
    )

    assert isinstance(summary, ReExtractionSummary)
    assert summary.success_ids == (a_ok.id,)
    assert summary.skipped_ids == (a_skip.id,)
    assert summary.failed_ids == (a_fail.id,)


# ---------------------------------------------------------------------------
# Noise skip 経路 (PR1-a 構造保証: ExtractionCall[Signal] のみ update 経路)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skips_when_re_extraction_returns_noise_and_keeps_signal_extraction(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """再抽出で Noise が返った場合は既存 ArticleExtraction を上書きしない。

    ``update_idempotent`` の signature が ``ExtractionCall[Signal]`` のみ
    受け付ける型 narrow を取っているため、Service 側 match で Noise は
    skipped に分類する。データ破壊防止の構造的保証 (signal table への
    noise 上書きを型レベルで排除、``feedback_structural_guarantee``)。
    """
    article = await _make_article(
        db_session, sample_source, "https://example.com/noise-skip"
    )
    parent = await _seed_extraction(
        db_session, article=article, entities=[("OldSignal", "company")]
    )
    parent_id_before = parent.id

    extractor = _extractor(
        return_value=_noise_call(entities=[("NoiseSurface", "Person")])
    )
    service = ReExtractionService(session_factory)
    summary = await service.execute((article.id,), extractor, dry_run=False)

    assert summary.skipped_ids == (article.id,)
    assert summary.success_ids == ()
    assert summary.failed_ids == ()

    # 既存 ArticleExtraction が上書きされていない
    async with session_factory() as fresh:
        parent_after = (
            await fresh.execute(
                select(ArticleExtraction).where(
                    ArticleExtraction.article_id == article.id
                )
            )
        ).scalar_one()
        assert parent_after.id == parent_id_before
        assert parent_after.translated_title == "旧タイトル"

        rows = (
            (
                await fresh.execute(
                    select(ArticleExtractionEntity).where(
                        ArticleExtractionEntity.extraction_id == parent_id_before
                    )
                )
            )
            .scalars()
            .all()
        )
    # 旧 entity ("OldSignal") のまま、Noise の "NoiseSurface" は永続化されていない
    assert [r.surface.root for r in rows] == ["OldSignal"]
