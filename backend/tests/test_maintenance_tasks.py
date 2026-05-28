"""back-fill cron タスクのテスト。

curation: kill switch / hold gate / 年齢削除 / 予算枯渇 / kiq 失敗続行を検証する。
hold gate (terminal_keep の性質で止まる) で運転中の停止を行う。年齢削除は実 DB
で監査 + 物理削除を検証する。
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.article import Article
from app.models.article_curation import ArticleCuration
from app.models.backfill_exclusion import (
    AssessmentBackfillExclusion,
    BackfillExclusionReason,
    EmbeddingBackfillExclusion,
)
from app.models.category import Category
from app.models.curation_noise import CurationNoise
from app.models.in_scope_assessment import InScopeAssessment
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent
from app.queue.helpers.backlog import BackfillTarget


def _ctx_with_session_factory() -> MagicMock:
    """ctx.state.session_factory を持つ Context モックを返す。"""
    ctx = MagicMock()
    ctx.state.session_factory = MagicMock()
    return ctx


def _stub_session_cm(ctx: MagicMock) -> None:
    """session_factory() の async context manager をモックする。"""
    ctx.state.session_factory.return_value.__aenter__ = AsyncMock(
        return_value=MagicMock()
    )
    ctx.state.session_factory.return_value.__aexit__ = AsyncMock(return_value=False)


def _target(
    target_id: int,
    *,
    article_id: int | None = None,
    source_name: str | None = "VentureBeat",
) -> BackfillTarget:
    """backfill enqueue 対象の test double を返す。"""
    return BackfillTarget(
        target_id=target_id,
        article_id=article_id if article_id is not None else target_id,
        source_name=source_name,
    )


# ---------------------------------------------------------------------------
# kill switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curations_disabled_returns_early() -> None:
    """kill switch False → hold 確認も backlog 参照も年齢削除もしない。"""
    from app.queue.tasks import backfill as tasks

    ctx = _ctx_with_session_factory()
    with (
        patch.object(tasks.settings, "backfill_curations_enabled", False),
        patch("app.queue.tasks.backfill.is_curation_held", new=AsyncMock()) as held,
        patch(
            "app.queue.tasks.backfill._delete_aged_out_curations",
            new=AsyncMock(return_value=0),
        ) as delete,
        patch("app.queue.tasks.backfill._append_backfill_run_event", new=AsyncMock()),
        patch("app.queue.tasks.backfill.PipelineBacklog") as backlog_cls,
    ):
        await tasks.backfill_curations(ctx=ctx)

    held.assert_not_called()
    delete.assert_not_called()
    backlog_cls.assert_not_called()


# ---------------------------------------------------------------------------
# hold gate — terminal_keep の hold 中は run 全体を skip (circuit breaker 差替)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curations_held_skips_entire_run() -> None:
    """hold 中は年齢削除も backlog 参照も dispatch も行わず即 return。"""
    from app.queue.tasks import backfill as tasks

    ctx = _ctx_with_session_factory()
    with (
        patch.object(tasks.settings, "backfill_curations_enabled", True),
        patch(
            "app.queue.tasks.backfill.is_curation_held",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.queue.tasks.backfill._delete_aged_out_curations",
            new=AsyncMock(return_value=0),
        ) as delete,
        patch("app.queue.tasks.backfill._append_backfill_run_event", new=AsyncMock()),
        patch("app.queue.tasks.backfill.PipelineBacklog") as backlog_cls,
        patch(
            "app.queue.tasks.backfill.consume_daily_budget", new=AsyncMock()
        ) as budget,
        patch("app.queue.tasks.backfill.curate_content") as curate_task,
    ):
        await tasks.backfill_curations(ctx=ctx)

    delete.assert_not_called()
    backlog_cls.assert_not_called()
    budget.assert_not_called()
    curate_task.kiq.assert_not_called()


# ---------------------------------------------------------------------------
# 空クエリ → 年齢削除は走るが dispatch なし
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curations_empty_does_not_dispatch() -> None:
    """backlog 空 → budget も kiq も呼ばない (年齢削除は実行される)。"""
    from app.queue.tasks import backfill as tasks

    ctx = _ctx_with_session_factory()
    _stub_session_cm(ctx)

    backlog_instance = MagicMock()
    backlog_instance.count_articles_pending_curation = AsyncMock(return_value=0)
    backlog_instance.curation_targets_pending = AsyncMock(return_value=[])

    with (
        patch.object(tasks.settings, "backfill_curations_enabled", True),
        patch(
            "app.queue.tasks.backfill.is_curation_held",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "app.queue.tasks.backfill._delete_aged_out_curations",
            new=AsyncMock(return_value=0),
        ) as delete,
        patch(
            "app.queue.tasks.backfill.PipelineBacklog", return_value=backlog_instance
        ),
        patch(
            "app.queue.tasks.backfill.consume_daily_budget", new=AsyncMock()
        ) as budget,
        patch("app.queue.tasks.backfill.curate_content") as curate_task,
    ):
        await tasks.backfill_curations(ctx=ctx)

    delete.assert_awaited_once()
    budget.assert_not_called()
    curate_task.kiq.assert_not_called()


# ---------------------------------------------------------------------------
# 日次予算枯渇 → kiq なし
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curations_budget_exhausted_skips_dispatch() -> None:
    """consume_daily_budget が 0 を返したら kiq dispatch せず終了。"""
    from app.queue.tasks import backfill as tasks

    ctx = _ctx_with_session_factory()
    _stub_session_cm(ctx)

    backlog_instance = MagicMock()
    backlog_instance.count_articles_pending_curation = AsyncMock(return_value=3)
    backlog_instance.curation_targets_pending = AsyncMock(
        return_value=[_target(1), _target(2), _target(3)]
    )

    with (
        patch.object(tasks.settings, "backfill_curations_enabled", True),
        patch(
            "app.queue.tasks.backfill.is_curation_held",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "app.queue.tasks.backfill._delete_aged_out_curations",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "app.queue.tasks.backfill.PipelineBacklog", return_value=backlog_instance
        ),
        patch(
            "app.queue.tasks.backfill.consume_daily_budget",
            new=AsyncMock(return_value=0),
        ),
        patch("app.queue.tasks.backfill.curate_content") as curate_task,
    ):
        await tasks.backfill_curations(ctx=ctx)

    curate_task.kiq.assert_not_called()


# ---------------------------------------------------------------------------
# dispatch — 対象 article_id を CurationTrigger で kiq
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curations_dispatches_triggers_for_each_article_id() -> None:
    """対象 article_id を ``CurationTrigger`` に詰めて kiq する (案 3)。

    precondition 判定 (article 既消滅 / 既処理) は下流 Stage 3 task に委譲。
    maintenance 層は ID-only Trigger を粛々と enqueue するだけの責務に縮約。
    """
    from app.queue.messages.curation import CurationTrigger
    from app.queue.tasks import backfill as tasks

    ctx = _ctx_with_session_factory()
    _stub_session_cm(ctx)

    backlog_instance = MagicMock()
    backlog_instance.count_articles_pending_curation = AsyncMock(return_value=3)
    backlog_instance.curation_targets_pending = AsyncMock(
        return_value=[_target(10), _target(20), _target(30)]
    )

    curate_task = MagicMock()
    curate_task.kiq = AsyncMock()

    with (
        patch.object(tasks.settings, "backfill_curations_enabled", True),
        patch(
            "app.queue.tasks.backfill.is_curation_held",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "app.queue.tasks.backfill._delete_aged_out_curations",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "app.queue.tasks.backfill.PipelineBacklog", return_value=backlog_instance
        ),
        patch(
            "app.queue.tasks.backfill.consume_daily_budget",
            new=AsyncMock(return_value=3),
        ),
        patch("app.queue.tasks.backfill.curate_content", curate_task),
    ):
        await tasks.backfill_curations(ctx=ctx)

    assert curate_task.kiq.await_count == 3
    dispatched = [call.args[0] for call in curate_task.kiq.await_args_list]
    assert dispatched == [
        CurationTrigger(article_id=10),
        CurationTrigger(article_id=20),
        CurationTrigger(article_id=30),
    ]


@pytest.mark.asyncio
async def test_curations_continues_when_one_kiq_fails() -> None:
    """1 件目 kiq が例外を上げても 2 件目以降は dispatch される。"""
    from app.queue.tasks import backfill as tasks

    ctx = _ctx_with_session_factory()
    _stub_session_cm(ctx)

    backlog_instance = MagicMock()
    backlog_instance.count_articles_pending_curation = AsyncMock(return_value=3)
    backlog_instance.curation_targets_pending = AsyncMock(
        return_value=[_target(1), _target(2), _target(3)]
    )

    curate_task = MagicMock()
    curate_task.kiq = AsyncMock(side_effect=[RuntimeError("queue down"), None, None])

    with (
        patch.object(tasks.settings, "backfill_curations_enabled", True),
        patch(
            "app.queue.tasks.backfill.is_curation_held",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "app.queue.tasks.backfill._delete_aged_out_curations",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "app.queue.tasks.backfill.PipelineBacklog", return_value=backlog_instance
        ),
        patch(
            "app.queue.tasks.backfill.consume_daily_budget",
            new=AsyncMock(return_value=3),
        ),
        patch("app.queue.tasks.backfill.curate_content", curate_task),
    ):
        await tasks.backfill_curations(ctx=ctx)

    assert curate_task.kiq.await_count == 3


# ---------------------------------------------------------------------------
# 年齢削除 (実 DB) — 監査 INSERT → 物理削除、noise は残す
# ---------------------------------------------------------------------------


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
        source_url=url,  # type: ignore[arg-type]
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
    return article


async def _make_curation(
    db_session: AsyncSession,
    article: Article,
) -> ArticleCuration:
    """テスト用 curation を作成する。"""
    curation = ArticleCuration(
        article_id=article.id,
        translated_title="tt",
        summary="ss",
    )
    db_session.add(curation)
    await db_session.commit()
    await db_session.refresh(curation)
    return curation


async def _make_in_scope_assessment(
    db_session: AsyncSession,
    curation: ArticleCuration,
    category: Category,
    *,
    embedding: list[float] | None = None,
) -> InScopeAssessment:
    """テスト用 in-scope assessment を作成する。"""
    assessment = InScopeAssessment(
        curation_id=curation.id,
        translated_title=curation.translated_title,
        summary=curation.summary,
        investor_take="it",
        category_id=category.id,
        embedding=embedding,
    )
    db_session.add(assessment)
    await db_session.commit()
    await db_session.refresh(assessment)
    return assessment


@pytest.mark.asyncio
async def test_delete_aged_out_curations_deletes_old_child_null_and_audits(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """古い child-NULL は監査を残して削除、noise を持つ古い記事は残す。"""
    from app.audit.stages.curation import BACKFILL_CURATION_AGED_OUT_CODE
    from app.queue.tasks import backfill as tasks

    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    aged = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/aged",
        created_at=now - timedelta(days=10),
    )
    kept = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/kept-noise",
        created_at=now - timedelta(days=10),
    )
    db_session.add(
        CurationNoise(article_id=kept.id, title_ja="ノイズ", summary_ja="ノイズ要約")
    )
    await db_session.commit()
    aged_id, kept_id = aged.id, kept.id

    deleted = await tasks._delete_aged_out_curations(
        session_factory, created_before=now - timedelta(days=7)
    )

    # 削除は別 session (session_factory) で commit 済。db_session の identity map を
    # 明示破棄して DB の最新状態を読む (rollback だけだと cached 値を返し得る)。
    db_session.expire_all()
    # child-NULL の古い記事は物理削除される
    assert await db_session.get(Article, aged_id) is None
    # noise (= 正常完了) を持つ記事は古くても残る (data-loss 防止)
    assert await db_session.get(Article, kept_id) is not None

    # 削除には監査が 1 行残る (article_id は FK SET NULL で NULL)
    events = list(
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.stage == "backfill_curate")
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "rejected"
    assert ev.outcome_code == BACKFILL_CURATION_AGED_OUT_CODE
    assert ev.retryability is None
    assert ev.article_id is None
    assert ev.payload["kind"] == "curation"
    assert deleted == 1


# ---------------------------------------------------------------------------
# Stage 4/5 年齢除外 (実 DB) — 監査 INSERT → soft exclude、業務 row は保持
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exclude_aged_out_assessments_keeps_article_and_audits(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """古い未 assessment curation は削除せず exclusion + audit を残す。"""
    from app.queue.tasks import backfill as tasks

    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/aged-assessment",
        created_at=now - timedelta(days=10),
    )
    curation = await _make_curation(db_session, article)
    article_id = article.id
    curation_id = curation.id
    source_name = str(sample_source.name)

    excluded = await tasks._exclude_aged_out_assessments(
        session_factory, created_before=now - timedelta(days=7)
    )

    db_session.expire_all()
    assert await db_session.get(Article, article_id) is not None
    assert await db_session.get(ArticleCuration, curation_id) is not None

    exclusion = await db_session.get(AssessmentBackfillExclusion, curation_id)
    assert exclusion is not None
    assert exclusion.reason_code == BackfillExclusionReason.ASSESSMENT_AGED_OUT.value

    events = list(
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.stage == "backfill_assess")
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "rejected"
    assert ev.outcome_code == BackfillExclusionReason.ASSESSMENT_AGED_OUT.value
    assert ev.retryability is None
    assert ev.article_id == article_id
    assert ev.payload["kind"] == "assessment"
    assert ev.payload["source_name"] == source_name
    assert ev.payload["curation_id"] == curation_id
    assert excluded == 1


@pytest.mark.asyncio
async def test_exclude_aged_out_assessments_skips_completed_race(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """helper 実行時点で assessment 済みなら exclusion / audit を作らない。"""
    from app.queue.tasks import backfill as tasks

    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/aged-assessment-done",
        created_at=now - timedelta(days=10),
    )
    curation = await _make_curation(db_session, article)
    await _make_in_scope_assessment(db_session, curation, sample_categories[0])

    excluded = await tasks._exclude_aged_out_assessments(
        session_factory, created_before=now - timedelta(days=7)
    )

    assert await db_session.get(AssessmentBackfillExclusion, curation.id) is None
    events = list(
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.stage == "backfill_assess")
            )
        )
        .scalars()
        .all()
    )
    assert events == []
    assert excluded == 0


@pytest.mark.asyncio
async def test_exclude_aged_out_embeddings_keeps_assessment_and_audits(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """古い embedding NULL analysis は削除せず exclusion + audit を残す。"""
    from app.queue.tasks import backfill as tasks

    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/aged-embedding",
        created_at=now - timedelta(days=10),
    )
    curation = await _make_curation(db_session, article)
    analysis = await _make_in_scope_assessment(
        db_session,
        curation,
        sample_categories[0],
    )
    article_id = article.id
    analysis_id = analysis.id

    excluded = await tasks._exclude_aged_out_embeddings(
        session_factory, created_before=now - timedelta(days=7)
    )

    db_session.expire_all()
    assert await db_session.get(InScopeAssessment, analysis_id) is not None

    exclusion = await db_session.get(EmbeddingBackfillExclusion, analysis_id)
    assert exclusion is not None
    assert exclusion.reason_code == BackfillExclusionReason.EMBEDDING_AGED_OUT.value

    events = list(
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.stage == "backfill_embed")
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "rejected"
    assert ev.outcome_code == BackfillExclusionReason.EMBEDDING_AGED_OUT.value
    assert ev.retryability is None
    assert ev.article_id == article_id
    assert ev.payload["kind"] == "embedding"
    assert ev.payload["analysis_id"] == analysis_id
    assert excluded == 1


@pytest.mark.asyncio
async def test_exclude_aged_out_embeddings_skips_completed_race(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    sample_categories: list[Category],
) -> None:
    """helper 実行時点で embedding 済みなら exclusion / audit を作らない。"""
    from app.queue.tasks import backfill as tasks

    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    article = await _make_article(
        db_session,
        sample_source,
        url="https://e.com/aged-embedding-done",
        created_at=now - timedelta(days=10),
    )
    curation = await _make_curation(db_session, article)
    analysis = await _make_in_scope_assessment(
        db_session,
        curation,
        sample_categories[0],
        embedding=[0.1] * 768,
    )

    excluded = await tasks._exclude_aged_out_embeddings(
        session_factory, created_before=now - timedelta(days=7)
    )

    assert await db_session.get(EmbeddingBackfillExclusion, analysis.id) is None
    events = list(
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.stage == "backfill_embed")
            )
        )
        .scalars()
        .all()
    )
    assert events == []
    assert excluded == 0


# ---------------------------------------------------------------------------
# assessments / embeddings の disabled パスも同様に early-return することの確認
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assessments_disabled_returns_early() -> None:
    from app.queue.tasks import backfill as tasks

    ctx = _ctx_with_session_factory()
    with (
        patch.object(tasks.settings, "backfill_assessments_enabled", False),
        patch("app.queue.tasks.backfill.is_assessment_held", new=AsyncMock()) as held,
        patch(
            "app.queue.tasks.backfill._exclude_aged_out_assessments",
            new=AsyncMock(return_value=0),
        ) as exclude,
        patch("app.queue.tasks.backfill._append_backfill_run_event", new=AsyncMock()),
        patch("app.queue.tasks.backfill.PipelineBacklog") as backlog_cls,
    ):
        await tasks.backfill_assessments(ctx=ctx)
    held.assert_not_called()
    exclude.assert_not_called()
    backlog_cls.assert_not_called()


@pytest.mark.asyncio
async def test_embeddings_disabled_returns_early() -> None:
    from app.queue.tasks import backfill as tasks

    ctx = _ctx_with_session_factory()
    with (
        patch.object(tasks.settings, "backfill_embeddings_enabled", False),
        patch("app.queue.tasks.backfill.is_embedding_held", new=AsyncMock()) as held,
        patch(
            "app.queue.tasks.backfill._exclude_aged_out_embeddings",
            new=AsyncMock(return_value=0),
        ) as exclude,
        patch("app.queue.tasks.backfill._append_backfill_run_event", new=AsyncMock()),
        patch("app.queue.tasks.backfill.PipelineBacklog") as backlog_cls,
    ):
        await tasks.backfill_embeddings(ctx=ctx)
    held.assert_not_called()
    exclude.assert_not_called()
    backlog_cls.assert_not_called()


@pytest.mark.asyncio
async def test_assessments_held_skips_entire_run() -> None:
    """assessment hold 中は backlog / budget / kiq に進まない。"""
    from app.queue.tasks import backfill as tasks

    ctx = _ctx_with_session_factory()
    with (
        patch.object(tasks.settings, "backfill_assessments_enabled", True),
        patch(
            "app.queue.tasks.backfill.is_assessment_held",
            new=AsyncMock(return_value=True),
        ) as held,
        patch(
            "app.queue.tasks.backfill._exclude_aged_out_assessments",
            new=AsyncMock(return_value=0),
        ) as exclude,
        patch("app.queue.tasks.backfill._append_backfill_run_event", new=AsyncMock()),
        patch("app.queue.tasks.backfill.PipelineBacklog") as backlog_cls,
        patch(
            "app.queue.tasks.backfill.consume_daily_budget", new=AsyncMock()
        ) as budget,
        patch("app.queue.tasks.backfill.assess_content") as assess_task,
    ):
        await tasks.backfill_assessments(ctx=ctx)

    held.assert_awaited_once()
    exclude.assert_not_called()
    backlog_cls.assert_not_called()
    budget.assert_not_called()
    assess_task.kiq.assert_not_called()


@pytest.mark.asyncio
async def test_embeddings_held_skips_entire_run() -> None:
    """embedding hold 中は backlog / budget / kiq に進まない。"""
    from app.queue.tasks import backfill as tasks

    ctx = _ctx_with_session_factory()
    with (
        patch.object(tasks.settings, "backfill_embeddings_enabled", True),
        patch(
            "app.queue.tasks.backfill.is_embedding_held",
            new=AsyncMock(return_value=True),
        ) as held,
        patch(
            "app.queue.tasks.backfill._exclude_aged_out_embeddings",
            new=AsyncMock(return_value=0),
        ) as exclude,
        patch("app.queue.tasks.backfill._append_backfill_run_event", new=AsyncMock()),
        patch("app.queue.tasks.backfill.PipelineBacklog") as backlog_cls,
        patch(
            "app.queue.tasks.backfill.consume_daily_budget", new=AsyncMock()
        ) as budget,
        patch("app.queue.tasks.backfill.generate_embedding") as embedding_task,
    ):
        await tasks.backfill_embeddings(ctx=ctx)

    held.assert_awaited_once()
    exclude.assert_not_called()
    backlog_cls.assert_not_called()
    budget.assert_not_called()
    embedding_task.kiq.assert_not_called()
