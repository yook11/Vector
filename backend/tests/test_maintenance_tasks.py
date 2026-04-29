"""back-fill cron タスクのユニットテスト。

kill switch / circuit / 予算枯渇 / kiq 失敗続行を検証する。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _ctx_with_session_factory() -> MagicMock:
    """ctx.state.session_factory を持つ Context モックを返す。"""
    ctx = MagicMock()
    ctx.state.session_factory = MagicMock()
    return ctx


# ---------------------------------------------------------------------------
# kill switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extractions_disabled_returns_early() -> None:
    """kill switch False のときは backlog 参照も circuit 更新もしない。"""
    from app.maintenance import tasks

    ctx = _ctx_with_session_factory()
    with (
        patch.object(tasks.settings, "backfill_extractions_enabled", False),
        patch("app.maintenance.tasks.PipelineBacklog") as backlog_cls,
        patch("app.maintenance.tasks._update_circuit_breaker") as circuit,
    ):
        await tasks.backfill_extractions(ctx=ctx)

    backlog_cls.assert_not_called()
    circuit.assert_not_called()


# ---------------------------------------------------------------------------
# 空クエリ → circuit リセット、kiq dispatch なし
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extractions_empty_resets_circuit_and_does_not_dispatch() -> None:
    """backlog 空 → circuit reset + kiq 未呼び出し。"""
    from app.maintenance import tasks

    ctx = _ctx_with_session_factory()
    fake_session = MagicMock()
    ctx.state.session_factory.return_value.__aenter__ = AsyncMock(
        return_value=fake_session
    )
    ctx.state.session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    backlog_instance = MagicMock()
    backlog_instance.article_ids_pending_extraction = AsyncMock(return_value=[])

    with (
        patch.object(tasks.settings, "backfill_extractions_enabled", True),
        patch("app.maintenance.tasks.PipelineBacklog", return_value=backlog_instance),
        patch(
            "app.maintenance.tasks._update_circuit_breaker",
            new=AsyncMock(return_value=0),
        ) as circuit,
        patch("app.maintenance.tasks.consume_daily_budget", new=AsyncMock()) as budget,
        patch("app.analysis.tasks.extract_content") as extract_task,
    ):
        await tasks.backfill_extractions(ctx=ctx)

    circuit.assert_awaited_once_with("extract", 0)
    budget.assert_not_called()
    extract_task.kiq.assert_not_called()


# ---------------------------------------------------------------------------
# circuit_open → 早期 return
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extractions_circuit_open_short_circuits() -> None:
    """streak が CIRCUIT_THRESHOLD 以上 → kiq dispatch せず early return。"""
    from app.maintenance import tasks

    ctx = _ctx_with_session_factory()
    ctx.state.session_factory.return_value.__aenter__ = AsyncMock(
        return_value=MagicMock()
    )
    ctx.state.session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    backlog_instance = MagicMock()
    backlog_instance.article_ids_pending_extraction = AsyncMock(return_value=[1, 2])

    with (
        patch.object(tasks.settings, "backfill_extractions_enabled", True),
        patch("app.maintenance.tasks.PipelineBacklog", return_value=backlog_instance),
        patch(
            "app.maintenance.tasks._update_circuit_breaker",
            new=AsyncMock(return_value=tasks.CIRCUIT_THRESHOLD),
        ),
        patch("app.maintenance.tasks.consume_daily_budget", new=AsyncMock()) as budget,
        patch("app.analysis.tasks.extract_content") as extract_task,
    ):
        await tasks.backfill_extractions(ctx=ctx)

    budget.assert_not_called()
    extract_task.kiq.assert_not_called()


# ---------------------------------------------------------------------------
# 日次予算枯渇 → kiq なし
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extractions_budget_exhausted_skips_dispatch() -> None:
    """consume_daily_budget が 0 を返したら kiq dispatch せず終了。"""
    from app.maintenance import tasks

    ctx = _ctx_with_session_factory()
    ctx.state.session_factory.return_value.__aenter__ = AsyncMock(
        return_value=MagicMock()
    )
    ctx.state.session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    backlog_instance = MagicMock()
    backlog_instance.article_ids_pending_extraction = AsyncMock(return_value=[1, 2, 3])

    with (
        patch.object(tasks.settings, "backfill_extractions_enabled", True),
        patch("app.maintenance.tasks.PipelineBacklog", return_value=backlog_instance),
        patch(
            "app.maintenance.tasks._update_circuit_breaker",
            new=AsyncMock(return_value=1),
        ),
        patch(
            "app.maintenance.tasks.consume_daily_budget",
            new=AsyncMock(return_value=0),
        ),
        patch("app.analysis.tasks.extract_content") as extract_task,
    ):
        await tasks.backfill_extractions(ctx=ctx)

    extract_task.kiq.assert_not_called()


# ---------------------------------------------------------------------------
# kiq 失敗 1 件 → 残り続行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extractions_continues_when_one_kiq_fails() -> None:
    """1 件目 kiq が例外を上げても 2 件目以降は dispatch される。"""
    from app.analysis.extraction.domain.ready import ReadyForExtraction
    from app.maintenance import tasks

    ctx = _ctx_with_session_factory()
    fake_article = MagicMock()
    fake_session = MagicMock()
    fake_session.get = AsyncMock(return_value=fake_article)
    ctx.state.session_factory.return_value.__aenter__ = AsyncMock(
        return_value=fake_session
    )
    ctx.state.session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    backlog_instance = MagicMock()
    backlog_instance.article_ids_pending_extraction = AsyncMock(return_value=[1, 2, 3])

    extract_task = MagicMock()
    extract_task.kiq = AsyncMock(side_effect=[RuntimeError("queue down"), None, None])

    ready = ReadyForExtraction(
        article_id=1, original_title="Title", original_content="content"
    )

    with (
        patch.object(tasks.settings, "backfill_extractions_enabled", True),
        patch("app.maintenance.tasks.PipelineBacklog", return_value=backlog_instance),
        patch(
            "app.maintenance.tasks._update_circuit_breaker",
            new=AsyncMock(return_value=1),
        ),
        patch(
            "app.maintenance.tasks.consume_daily_budget",
            new=AsyncMock(return_value=3),
        ),
        patch(
            "app.analysis.extraction.domain.ready.ReadyForExtraction.try_advance_from",
            new=AsyncMock(return_value=ready),
        ),
        patch("app.analysis.tasks.extract_content", extract_task),
    ):
        await tasks.backfill_extractions(ctx=ctx)

    assert extract_task.kiq.await_count == 3


# ---------------------------------------------------------------------------
# Pattern A' gatekeeper: article 不在 / try_advance_from None で skip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extractions_skips_when_article_missing() -> None:
    """session.get が None なら kiq せず skip する。"""
    from app.maintenance import tasks

    ctx = _ctx_with_session_factory()
    fake_session = MagicMock()
    fake_session.get = AsyncMock(return_value=None)
    ctx.state.session_factory.return_value.__aenter__ = AsyncMock(
        return_value=fake_session
    )
    ctx.state.session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    backlog_instance = MagicMock()
    backlog_instance.article_ids_pending_extraction = AsyncMock(return_value=[42])

    extract_task = MagicMock()
    extract_task.kiq = AsyncMock()

    with (
        patch.object(tasks.settings, "backfill_extractions_enabled", True),
        patch("app.maintenance.tasks.PipelineBacklog", return_value=backlog_instance),
        patch(
            "app.maintenance.tasks._update_circuit_breaker",
            new=AsyncMock(return_value=1),
        ),
        patch(
            "app.maintenance.tasks.consume_daily_budget",
            new=AsyncMock(return_value=1),
        ),
        patch("app.analysis.tasks.extract_content", extract_task),
    ):
        await tasks.backfill_extractions(ctx=ctx)

    extract_task.kiq.assert_not_called()


@pytest.mark.asyncio
async def test_extractions_skips_when_advance_returns_none() -> None:
    """ReadyForExtraction.try_advance_from が None なら kiq せず skip する。"""
    from app.maintenance import tasks

    ctx = _ctx_with_session_factory()
    fake_article = MagicMock()
    fake_session = MagicMock()
    fake_session.get = AsyncMock(return_value=fake_article)
    ctx.state.session_factory.return_value.__aenter__ = AsyncMock(
        return_value=fake_session
    )
    ctx.state.session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    backlog_instance = MagicMock()
    backlog_instance.article_ids_pending_extraction = AsyncMock(return_value=[42])

    extract_task = MagicMock()
    extract_task.kiq = AsyncMock()

    with (
        patch.object(tasks.settings, "backfill_extractions_enabled", True),
        patch("app.maintenance.tasks.PipelineBacklog", return_value=backlog_instance),
        patch(
            "app.maintenance.tasks._update_circuit_breaker",
            new=AsyncMock(return_value=1),
        ),
        patch(
            "app.maintenance.tasks.consume_daily_budget",
            new=AsyncMock(return_value=1),
        ),
        patch(
            "app.analysis.extraction.domain.ready.ReadyForExtraction.try_advance_from",
            new=AsyncMock(return_value=None),
        ),
        patch("app.analysis.tasks.extract_content", extract_task),
    ):
        await tasks.backfill_extractions(ctx=ctx)

    extract_task.kiq.assert_not_called()


# ---------------------------------------------------------------------------
# classifications / embeddings の disabled パスも同様に early-return することの確認
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classifications_disabled_returns_early() -> None:
    from app.maintenance import tasks

    ctx = _ctx_with_session_factory()
    with (
        patch.object(tasks.settings, "backfill_classifications_enabled", False),
        patch("app.maintenance.tasks.PipelineBacklog") as backlog_cls,
    ):
        await tasks.backfill_classifications(ctx=ctx)
    backlog_cls.assert_not_called()


@pytest.mark.asyncio
async def test_embeddings_disabled_returns_early() -> None:
    from app.maintenance import tasks

    ctx = _ctx_with_session_factory()
    with (
        patch.object(tasks.settings, "backfill_embeddings_enabled", False),
        patch("app.maintenance.tasks.PipelineBacklog") as backlog_cls,
    ):
        await tasks.backfill_embeddings(ctx=ctx)
    backlog_cls.assert_not_called()
