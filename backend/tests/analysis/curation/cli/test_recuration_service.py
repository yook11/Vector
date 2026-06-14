"""RecurationService 統合テスト (Phase 1B α-1)。

検証する観点:

- 新規 article_id (AnalyzableArticleRecord 不在) → ``skipped_ids``
- ArticleCuration 不在 (AnalyzableArticleRecord のみ) → ``skipped_ids``
- 正常: 既存 extraction が UPDATE され、子 entity が差し替わる → ``success_ids``
- dry_run=True: extractor は呼ばれるが DB は変更されない (rollback)
- ``CurationTerminalDropError`` (ACL 詰め替え後の
  ``AIProviderInputRejectedError``) → ``skipped_ids``
  (failed には入らない、通常 pipeline でも記事 DELETE 対象のカテゴリ)
- ``CurationTerminalKeepError`` (ACL 詰め替え後の
  ``AIProviderConfigurationError``) → ``failed_ids``
  (1 回試行で即 failed、retry を消費しない)
- ``CurationRecoverableError`` (ACL 詰め替え後の ``AIProviderNetworkError``)
  を retry 上限まで → ``failed_ids``
- ``AIProviderNetworkError`` 1 回 → 成功 → ``success_ids``
  (retry すれば成功するパターン)
- 親 ``ArticleCuration.id`` は保持される (CASCADE 連鎖防止の構造保証)
- 再抽出で Noise が返った場合は ``skipped_ids`` に分類され、既存
  ``ArticleCuration`` は上書きされない (データ破壊防止の構造保証)

extractor は ``unittest.mock`` で差し替え (実 Gemini を呼ばない)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderInputRejectedError,
    AIProviderNetworkError,
)
from app.analysis.curation.ai.base import BaseCurator
from app.analysis.curation.ai.envelope import CurationCall
from app.analysis.curation.cli.recuration_service import (
    RecurationService,
    RecurationSummary,
)
from app.analysis.curation.domain import Noise, Signal
from app.analysis.curation.repository import CurationRepository
from app.analysis.gemini_error_translator import GeminiContentRejectionReason
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.news_source import NewsSource


def _signal_call(
    *,
    title_ja: str = "新タイトル",
    summary_ja: str = "新要約",
) -> CurationCall[Signal]:
    """``CurationCall[Signal]`` を生成するヘルパー。"""
    return CurationCall(
        result=Signal(title_ja=title_ja, summary_ja=summary_ja),
        raw_response='{"relevance":"signal"}',
        raw_relevance="signal",
        prompt_version="testver1",
        model_name="test-model-x",
    )


def _noise_call(
    *,
    title_ja: str = "ノイズタイトル",
    summary_ja: str = "ノイズ要約",
) -> CurationCall[Noise]:
    """``CurationCall[Noise]`` を生成するヘルパー (再抽出で Noise 経路を作る用)。"""
    return CurationCall(
        result=Noise(title_ja=title_ja, summary_ja=summary_ja),
        raw_response='{"relevance":"noise"}',
        raw_relevance="noise",
        prompt_version="testver1",
        model_name="test-model-x",
    )


def _curator(
    *,
    return_value: CurationCall[Signal] | CurationCall[Noise] | None = None,
    side_effect=None,
) -> BaseCurator:
    """``BaseCurator`` の最小モック (model_name + extract のみ)。"""
    mock = MagicMock(spec=BaseCurator)
    type(mock).model_name = "test-model-x"
    if side_effect is not None:
        mock.curate = AsyncMock(side_effect=side_effect)
    else:
        mock.curate = AsyncMock(return_value=return_value or _signal_call())
    return mock


async def _make_article(
    db_session: AsyncSession, sample_source: NewsSource, url: str
) -> AnalyzableArticleRecord:
    article = AnalyzableArticleRecord(
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
    article: AnalyzableArticleRecord,
) -> ArticleCuration:
    """AnalyzableArticleRecord + 既存 ArticleCuration を作る。"""
    repo = CurationRepository(db_session)
    saved = await repo.save_signal(
        _signal_call(title_ja="旧タイトル", summary_ja="旧要約"),
        analyzable_article_id=article.id,
    )
    await db_session.commit()
    assert saved is not None
    parent = (
        await db_session.execute(
            select(ArticleCuration).where(
                ArticleCuration.analyzable_article_id == article.id
            )
        )
    ).scalar_one()
    return parent


# skip 経路


@pytest.mark.asyncio
async def test_skips_when_article_does_not_exist(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = RecurationService(session_factory)
    summary = await service.execute((999_999,), _curator(), dry_run=False)
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
    service = RecurationService(session_factory)
    summary = await service.execute((article.id,), _curator(), dry_run=False)
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
    await _seed_extraction(db_session, article=article)

    curator = _curator(
        side_effect=AIProviderInputRejectedError(
            reason=GeminiContentRejectionReason.INPUT_BLOCKED
        )
    )
    service = RecurationService(session_factory)
    summary = await service.execute((article.id,), curator, dry_run=False)

    assert summary.skipped_ids == (article.id,)
    assert summary.failed_ids == ()


@pytest.mark.asyncio
async def test_success_updates_parent_in_place_keeps_id(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """re-extraction 成功時: parent id は変わらず translated_title だけ差し替わる。"""
    article = await _make_article(
        db_session, sample_source, "https://example.com/success"
    )
    parent = await _seed_extraction(db_session, article=article)
    parent_id_before = parent.id

    curator = _curator(return_value=_signal_call())
    service = RecurationService(session_factory)
    summary = await service.execute((article.id,), curator, dry_run=False)

    assert summary.success_ids == (article.id,)
    assert summary.dry_run is False

    async with session_factory() as fresh:
        parent_after = (
            await fresh.execute(
                select(ArticleCuration).where(
                    ArticleCuration.analyzable_article_id == article.id
                )
            )
        ).scalar_one()
        assert parent_after.id == parent_id_before
        assert parent_after.translated_title == "新タイトル"


@pytest.mark.asyncio
async def test_dry_run_calls_curator_but_rolls_back(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """dry_run=True: extractor は呼ばれるが DB は変更されない。"""
    article = await _make_article(db_session, sample_source, "https://example.com/dry")
    await _seed_extraction(db_session, article=article)

    curator = _curator(return_value=_signal_call())
    service = RecurationService(session_factory)
    summary = await service.execute((article.id,), curator, dry_run=True)

    assert summary.success_ids == (article.id,)
    assert summary.dry_run is True
    curator.curate.assert_awaited_once()

    async with session_factory() as fresh:
        parent = (
            await fresh.execute(
                select(ArticleCuration).where(
                    ArticleCuration.analyzable_article_id == article.id
                )
            )
        ).scalar_one()
        # UPDATE が roll back されたので旧タイトルのまま
        assert parent.translated_title == "旧タイトル"


# retry / failed


@pytest.mark.asyncio
async def test_retries_then_succeeds(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """AIProviderNetworkError 1 回 → 成功で success_ids に入る。"""
    article = await _make_article(
        db_session, sample_source, "https://example.com/retry"
    )
    await _seed_extraction(db_session, article=article)

    curator = _curator(
        side_effect=[AIProviderNetworkError("transient"), _signal_call()]
    )
    service = RecurationService(session_factory, max_retries=3)
    summary = await service.execute((article.id,), curator, dry_run=False)

    assert summary.success_ids == (article.id,)
    assert curator.curate.await_count == 2


@pytest.mark.asyncio
async def test_failed_after_max_retries(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """AIProviderNetworkError が max_retries 回連続で failed_ids に入る。"""
    article = await _make_article(
        db_session, sample_source, "https://example.com/failed"
    )
    await _seed_extraction(db_session, article=article)

    curator = _curator(side_effect=AIProviderNetworkError("dead"))
    service = RecurationService(session_factory, max_retries=2)
    summary = await service.execute((article.id,), curator, dry_run=False)

    assert summary.failed_ids == (article.id,)
    assert summary.success_ids == ()
    assert curator.curate.await_count == 2


@pytest.mark.asyncio
async def test_configuration_error_fails_immediately_without_retry(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """``CurationTerminalKeepError`` (ACL 詰め替え後の
    ``AIProviderConfigurationError``) は 1 回試行で即 failed。

    retry しても解消しない種類のエラーなので ``max_retries`` を消費しない。
    本番 task chain と同じ dispatch 軸 (Stage 3 marker) に揃えた結果の挙動。
    """
    article = await _make_article(
        db_session, sample_source, "https://example.com/config-fail"
    )
    await _seed_extraction(db_session, article=article)

    curator = _curator(side_effect=AIProviderConfigurationError("api key invalid"))
    service = RecurationService(session_factory, max_retries=3)
    summary = await service.execute((article.id,), curator, dry_run=False)

    assert summary.failed_ids == (article.id,)
    assert summary.success_ids == ()
    assert summary.skipped_ids == ()
    assert curator.curate.await_count == 1  # retry されていない


@pytest.mark.asyncio
async def test_summary_aggregates_per_article_independently(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """1 件 success / 1 件 skip (no extraction) / 1 件 failed が独立に集約される。"""
    a_ok = await _make_article(db_session, sample_source, "https://example.com/ok")
    await _seed_extraction(db_session, article=a_ok)
    a_skip = await _make_article(db_session, sample_source, "https://example.com/skip")
    a_fail = await _make_article(db_session, sample_source, "https://example.com/fail")
    await _seed_extraction(db_session, article=a_fail)

    call_log: list[int] = []

    async def _curate_side_effect(
        *, title: str, content: str
    ) -> CurationCall[Signal] | CurationCall[Noise]:
        call_log.append(len(call_log))
        # 順序: a_ok → a_fail (a_skip は extract まで来ない)
        if len(call_log) == 1:
            return _signal_call()
        raise AIProviderNetworkError("dead")

    curator = MagicMock(spec=BaseCurator)
    type(curator).model_name = "test-model-x"
    curator.curate = AsyncMock(side_effect=_curate_side_effect)

    service = RecurationService(session_factory, max_retries=1)
    summary = await service.execute(
        (a_ok.id, a_skip.id, a_fail.id), curator, dry_run=False
    )

    assert isinstance(summary, RecurationSummary)
    assert summary.success_ids == (a_ok.id,)
    assert summary.skipped_ids == (a_skip.id,)
    assert summary.failed_ids == (a_fail.id,)


# Noise skip 経路 (PR1-a 構造保証: CurationCall[Signal] のみ update 経路)


@pytest.mark.asyncio
async def test_skips_when_re_extraction_returns_noise_and_keeps_signal_extraction(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """再抽出で Noise が返った場合は既存 ArticleCuration を上書きしない。

    ``update_signal_idempotent`` の signature が ``CurationCall[Signal]`` のみ
    受け付ける型 narrow を取っているため、Service 側 match で Noise は
    skipped に分類する。データ破壊防止の構造的保証 (signal table への
    noise 上書きを型レベルで排除、``feedback_structural_guarantee``)。
    """
    article = await _make_article(
        db_session, sample_source, "https://example.com/noise-skip"
    )
    parent = await _seed_extraction(db_session, article=article)
    parent_id_before = parent.id

    curator = _curator(return_value=_noise_call())
    service = RecurationService(session_factory)
    summary = await service.execute((article.id,), curator, dry_run=False)

    assert summary.skipped_ids == (article.id,)
    assert summary.success_ids == ()
    assert summary.failed_ids == ()

    # 既存 ArticleCuration が上書きされていない
    async with session_factory() as fresh:
        parent_after = (
            await fresh.execute(
                select(ArticleCuration).where(
                    ArticleCuration.analyzable_article_id == article.id
                )
            )
        ).scalar_one()
        assert parent_after.id == parent_id_before
        assert parent_after.translated_title == "旧タイトル"
