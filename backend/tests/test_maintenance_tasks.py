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
async def test_curations_disabled_returns_early() -> None:
    """kill switch False のときは backlog 参照も circuit 更新もしない。"""
    from app.maintenance import tasks

    ctx = _ctx_with_session_factory()
    with (
        patch.object(tasks.settings, "backfill_curations_enabled", False),
        patch("app.maintenance.tasks.PipelineBacklog") as backlog_cls,
        patch("app.maintenance.tasks._update_circuit_breaker") as circuit,
    ):
        await tasks.backfill_curations(ctx=ctx)

    backlog_cls.assert_not_called()
    circuit.assert_not_called()


# ---------------------------------------------------------------------------
# 空クエリ → circuit リセット、kiq dispatch なし
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curations_empty_resets_circuit_and_does_not_dispatch() -> None:
    """backlog 空 → circuit reset + kiq 未呼び出し。"""
    from app.maintenance import tasks

    ctx = _ctx_with_session_factory()
    fake_session = MagicMock()
    ctx.state.session_factory.return_value.__aenter__ = AsyncMock(
        return_value=fake_session
    )
    ctx.state.session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    backlog_instance = MagicMock()
    backlog_instance.article_ids_pending_curation = AsyncMock(return_value=[])

    with (
        patch.object(tasks.settings, "backfill_curations_enabled", True),
        patch("app.maintenance.tasks.PipelineBacklog", return_value=backlog_instance),
        patch(
            "app.maintenance.tasks._update_circuit_breaker",
            new=AsyncMock(return_value=0),
        ) as circuit,
        patch("app.maintenance.tasks.consume_daily_budget", new=AsyncMock()) as budget,
        patch("app.analysis.curation.tasks.curate_content") as curate_task,
    ):
        await tasks.backfill_curations(ctx=ctx)

    circuit.assert_awaited_once_with("curate", 0)
    budget.assert_not_called()
    curate_task.kiq.assert_not_called()


# ---------------------------------------------------------------------------
# circuit_open → 早期 return
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curations_circuit_open_short_circuits() -> None:
    """streak が CIRCUIT_THRESHOLD 以上 → kiq dispatch せず early return。"""
    from app.maintenance import tasks

    ctx = _ctx_with_session_factory()
    ctx.state.session_factory.return_value.__aenter__ = AsyncMock(
        return_value=MagicMock()
    )
    ctx.state.session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    backlog_instance = MagicMock()
    backlog_instance.article_ids_pending_curation = AsyncMock(return_value=[1, 2])

    with (
        patch.object(tasks.settings, "backfill_curations_enabled", True),
        patch("app.maintenance.tasks.PipelineBacklog", return_value=backlog_instance),
        patch(
            "app.maintenance.tasks._update_circuit_breaker",
            new=AsyncMock(return_value=tasks.CIRCUIT_THRESHOLD),
        ),
        patch("app.maintenance.tasks.consume_daily_budget", new=AsyncMock()) as budget,
        patch("app.analysis.curation.tasks.curate_content") as curate_task,
    ):
        await tasks.backfill_curations(ctx=ctx)

    budget.assert_not_called()
    curate_task.kiq.assert_not_called()


# ---------------------------------------------------------------------------
# 日次予算枯渇 → kiq なし
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curations_budget_exhausted_skips_dispatch() -> None:
    """consume_daily_budget が 0 を返したら kiq dispatch せず終了。"""
    from app.maintenance import tasks

    ctx = _ctx_with_session_factory()
    ctx.state.session_factory.return_value.__aenter__ = AsyncMock(
        return_value=MagicMock()
    )
    ctx.state.session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    backlog_instance = MagicMock()
    backlog_instance.article_ids_pending_curation = AsyncMock(return_value=[1, 2, 3])

    with (
        patch.object(tasks.settings, "backfill_curations_enabled", True),
        patch("app.maintenance.tasks.PipelineBacklog", return_value=backlog_instance),
        patch(
            "app.maintenance.tasks._update_circuit_breaker",
            new=AsyncMock(return_value=1),
        ),
        patch(
            "app.maintenance.tasks.consume_daily_budget",
            new=AsyncMock(return_value=0),
        ),
        patch("app.analysis.curation.tasks.curate_content") as curate_task,
    ):
        await tasks.backfill_curations(ctx=ctx)

    curate_task.kiq.assert_not_called()


# ---------------------------------------------------------------------------
# kiq 失敗 1 件 → 残り続行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curations_dispatches_triggers_for_each_article_id() -> None:
    """対象 article_id を ``CurationTrigger`` に詰めて kiq する (案 3)。

    precondition 判定 (article 既消滅 / 既処理) は下流 Stage 3 task に委譲。
    maintenance 層は ID-only Trigger を粛々と enqueue するだけの責務に縮約。
    """
    from app.analysis.curation.domain.ready import CurationTrigger
    from app.maintenance import tasks

    ctx = _ctx_with_session_factory()
    ctx.state.session_factory.return_value.__aenter__ = AsyncMock(
        return_value=MagicMock()
    )
    ctx.state.session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    backlog_instance = MagicMock()
    backlog_instance.article_ids_pending_curation = AsyncMock(return_value=[10, 20, 30])

    curate_task = MagicMock()
    curate_task.kiq = AsyncMock()

    with (
        patch.object(tasks.settings, "backfill_curations_enabled", True),
        patch("app.maintenance.tasks.PipelineBacklog", return_value=backlog_instance),
        patch(
            "app.maintenance.tasks._update_circuit_breaker",
            new=AsyncMock(return_value=1),
        ),
        patch(
            "app.maintenance.tasks.consume_daily_budget",
            new=AsyncMock(return_value=3),
        ),
        patch("app.analysis.curation.tasks.curate_content", curate_task),
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
    from app.maintenance import tasks

    ctx = _ctx_with_session_factory()
    ctx.state.session_factory.return_value.__aenter__ = AsyncMock(
        return_value=MagicMock()
    )
    ctx.state.session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    backlog_instance = MagicMock()
    backlog_instance.article_ids_pending_curation = AsyncMock(return_value=[1, 2, 3])

    curate_task = MagicMock()
    curate_task.kiq = AsyncMock(side_effect=[RuntimeError("queue down"), None, None])

    with (
        patch.object(tasks.settings, "backfill_curations_enabled", True),
        patch("app.maintenance.tasks.PipelineBacklog", return_value=backlog_instance),
        patch(
            "app.maintenance.tasks._update_circuit_breaker",
            new=AsyncMock(return_value=1),
        ),
        patch(
            "app.maintenance.tasks.consume_daily_budget",
            new=AsyncMock(return_value=3),
        ),
        patch("app.analysis.curation.tasks.curate_content", curate_task),
    ):
        await tasks.backfill_curations(ctx=ctx)

    assert curate_task.kiq.await_count == 3


# ---------------------------------------------------------------------------
# assessments / embeddings の disabled パスも同様に early-return することの確認
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assessments_disabled_returns_early() -> None:
    from app.maintenance import tasks

    ctx = _ctx_with_session_factory()
    with (
        patch.object(tasks.settings, "backfill_assessments_enabled", False),
        patch("app.maintenance.tasks.PipelineBacklog") as backlog_cls,
    ):
        await tasks.backfill_assessments(ctx=ctx)
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
