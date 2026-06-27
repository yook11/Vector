"""backfill run / enqueue audit の task orchestration tests。pipeline_stage span 配線テスト含む。"""  # noqa: E501

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from logfire.testing import CaptureLogfire

from app.audit.domain.event import Stage
from app.audit.stages.backfill import BackfillOutcomeCode
from app.queue.helpers.backlog import BackfillTarget
from app.queue.tasks import backfill as tasks
from tests.logfire._span_helpers import pipeline_stage_attrs


@dataclass(frozen=True, slots=True)
class _TaskCase:
    name: str
    task: Callable[[Any], Awaitable[None]]
    enabled_attr: str
    hold_patch: str
    ageout_patch: str
    queue_task_patch: str
    count_method: str
    target_method: str
    budget_role: str
    backfill_stage: str
    target_kind: str
    limit: int
    daily_max: int


CASES = [
    _TaskCase(
        name="curate",
        task=tasks.backfill_curations,
        enabled_attr="backfill_curations_enabled",
        hold_patch="app.queue.tasks.backfill.is_curation_held",
        ageout_patch="app.queue.tasks.backfill._delete_aged_out_curations",
        queue_task_patch="app.queue.tasks.backfill.curate_content",
        count_method="count_articles_pending_curation",
        target_method="curation_targets_pending",
        budget_role="curate",
        backfill_stage="curate",
        target_kind="article",
        limit=tasks.CURATIONS_LIMIT,
        daily_max=tasks.CURATIONS_DAILY_MAX,
    ),
    _TaskCase(
        name="assess",
        task=tasks.backfill_assessments,
        enabled_attr="backfill_assessments_enabled",
        hold_patch="app.queue.tasks.backfill.is_assessment_held",
        ageout_patch="app.queue.tasks.backfill._exclude_aged_out_assessments",
        queue_task_patch="app.queue.tasks.backfill.assess_content",
        count_method="count_curations_pending_assessment",
        target_method="assessment_targets_pending",
        budget_role="assess",
        backfill_stage="assess",
        target_kind="curation",
        limit=tasks.ASSESSMENTS_LIMIT,
        daily_max=tasks.ASSESSMENTS_DAILY_MAX,
    ),
    _TaskCase(
        name="embed",
        task=tasks.backfill_embeddings,
        enabled_attr="backfill_embeddings_enabled",
        hold_patch="app.queue.tasks.backfill.is_embedding_held",
        ageout_patch="app.queue.tasks.backfill._exclude_aged_out_embeddings",
        queue_task_patch="app.queue.tasks.backfill.generate_embedding",
        count_method="count_analyzed_articles_pending_embedding",
        target_method="embedding_targets_pending",
        budget_role="embed",
        backfill_stage="embed",
        target_kind="analyzed_article",
        limit=tasks.EMBEDDINGS_LIMIT,
        daily_max=tasks.EMBEDDINGS_DAILY_MAX,
    ),
]


def _ctx() -> SimpleNamespace:
    """session_factory だけを持つ taskiq Context test double。"""
    return SimpleNamespace(state=SimpleNamespace(session_factory=MagicMock()))


def _target(target_id: int) -> BackfillTarget:
    """backfill enqueue 対象の test double を返す。"""
    return BackfillTarget(
        target_id=target_id,
        analyzable_article_id=target_id + 1000,
        source_name="VentureBeat",
    )


def _run_outcomes(audit: AsyncMock) -> list[BackfillOutcomeCode]:
    """run audit mock に渡された outcome_code 一覧。"""
    return [call.kwargs["outcome_code"] for call in audit.await_args_list]


def _item_outcomes(audit: AsyncMock) -> list[BackfillOutcomeCode]:
    """item audit mock に渡された outcome_code 一覧。"""
    return [call.kwargs["outcome_code"] for call in audit.await_args_list]


def _assert_run_audit_derives_stage_from_backfill_stage(
    audit: AsyncMock, case: _TaskCase
) -> None:
    kwargs = audit.await_args.kwargs
    assert "stage" not in kwargs
    assert kwargs["backfill_stage"] == case.backfill_stage


def _assert_item_audit_derives_stage_from_backfill_stage(
    audit: AsyncMock, case: _TaskCase
) -> None:
    for call in audit.await_args_list:
        assert "stage" not in call.kwargs
        assert call.kwargs["backfill_stage"] == case.backfill_stage


@pytest.mark.parametrize(
    "wrapper",
    [tasks._append_backfill_item_event, tasks._append_backfill_run_event],
)
def test_backfill_audit_task_wrappers_do_not_accept_stage(wrapper: object) -> None:
    """task-level wrapper も caller-provided stage を受け取らない。"""
    params = inspect.signature(wrapper).parameters

    assert "stage" not in params
    assert "backfill_stage" in params


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
async def test_kill_switch_disabled_is_audited(case: _TaskCase) -> None:
    """kill switch false は run skipped として監査され、selection に進まない。"""
    run_audit = AsyncMock()
    with (
        patch.object(tasks.settings, case.enabled_attr, False),
        patch(case.hold_patch, AsyncMock()) as held,
        patch("app.queue.tasks.backfill.PipelineBacklog") as backlog_cls,
        patch("app.queue.tasks.backfill._append_backfill_run_event", run_audit),
    ):
        await case.task(ctx=_ctx())

    held.assert_not_called()
    backlog_cls.assert_not_called()
    assert _run_outcomes(run_audit) == [BackfillOutcomeCode.RUN_KILL_SWITCH_DISABLED]
    _assert_run_audit_derives_stage_from_backfill_stage(run_audit, case)
    # daily_max は budget exhausted 専用 (停止閾値)。他 skip では焼かない。
    assert "daily_max" not in run_audit.await_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
async def test_stage_hold_is_audited(case: _TaskCase) -> None:
    """hold 中は run skipped として監査され、selection に進まない。"""
    run_audit = AsyncMock()
    with (
        patch.object(tasks.settings, case.enabled_attr, True),
        patch(case.hold_patch, AsyncMock(return_value=True)),
        patch(case.ageout_patch, AsyncMock(return_value=0)) as ageout,
        patch("app.queue.tasks.backfill.PipelineBacklog") as backlog_cls,
        patch("app.queue.tasks.backfill._append_backfill_run_event", run_audit),
    ):
        await case.task(ctx=_ctx())

    ageout.assert_not_called()
    backlog_cls.assert_not_called()
    assert _run_outcomes(run_audit) == [BackfillOutcomeCode.RUN_HELD_BY_STAGE_HOLD]
    _assert_run_audit_derives_stage_from_backfill_stage(run_audit, case)
    assert "daily_max" not in run_audit.await_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
async def test_no_targets_is_audited(case: _TaskCase) -> None:
    """対象 0 件は run no-targets として監査される。"""
    backlog = MagicMock()
    setattr(backlog, case.target_method, AsyncMock(return_value=[]))
    setattr(backlog, case.count_method, AsyncMock(return_value=0))
    run_audit = AsyncMock()

    with (
        patch.object(tasks.settings, case.enabled_attr, True),
        patch(case.hold_patch, AsyncMock(return_value=False)),
        patch(case.ageout_patch, AsyncMock(return_value=0)),
        patch("app.queue.tasks.backfill.PipelineBacklog", return_value=backlog),
        patch("app.queue.tasks.backfill.consume_daily_budget", AsyncMock()) as budget,
        patch("app.queue.tasks.backfill._append_backfill_run_event", run_audit),
    ):
        await case.task(ctx=_ctx())

    budget.assert_not_called()
    assert _run_outcomes(run_audit) == [BackfillOutcomeCode.RUN_NO_TARGETS]
    _assert_run_audit_derives_stage_from_backfill_stage(run_audit, case)
    assert "daily_max" not in run_audit.await_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
async def test_budget_exhausted_is_audited(case: _TaskCase) -> None:
    """daily budget 0 は run budget-exhausted として監査される。"""
    targets = [_target(1), _target(2)]
    backlog = MagicMock()
    setattr(backlog, case.target_method, AsyncMock(return_value=targets))
    setattr(backlog, case.count_method, AsyncMock(return_value=len(targets)))
    run_audit = AsyncMock()

    with (
        patch.object(tasks.settings, case.enabled_attr, True),
        patch(case.hold_patch, AsyncMock(return_value=False)),
        patch(case.ageout_patch, AsyncMock(return_value=0)),
        patch("app.queue.tasks.backfill.PipelineBacklog", return_value=backlog),
        patch(
            "app.queue.tasks.backfill.consume_daily_budget",
            AsyncMock(return_value=0),
        ),
        patch(case.queue_task_patch) as queue_task,
        patch("app.queue.tasks.backfill._append_backfill_run_event", run_audit),
    ):
        await case.task(ctx=_ctx())

    queue_task.kiq.assert_not_called()
    assert _run_outcomes(run_audit) == [BackfillOutcomeCode.RUN_DAILY_BUDGET_EXHAUSTED]
    _assert_run_audit_derives_stage_from_backfill_stage(run_audit, case)
    # 停止閾値 daily_max は budget exhausted event でのみ KEEP。
    assert run_audit.await_args.kwargs["daily_max"] == case.daily_max


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
async def test_enqueue_success_items_are_audited_and_no_run_summary(
    case: _TaskCase,
) -> None:
    """成功 item は監査され、run summary は焼かれない (保証1)。"""
    targets = [_target(1), _target(2)]
    backlog = MagicMock()
    setattr(backlog, case.target_method, AsyncMock(return_value=targets))
    setattr(backlog, case.count_method, AsyncMock(return_value=len(targets)))
    run_audit = AsyncMock()
    item_audit = AsyncMock()
    queue_task = SimpleNamespace(kiq=AsyncMock())

    with (
        patch.object(tasks.settings, case.enabled_attr, True),
        patch(case.hold_patch, AsyncMock(return_value=False)),
        patch(case.ageout_patch, AsyncMock(return_value=0)),
        patch("app.queue.tasks.backfill.PipelineBacklog", return_value=backlog),
        patch(
            "app.queue.tasks.backfill.consume_daily_budget",
            AsyncMock(return_value=len(targets)),
        ),
        patch(case.queue_task_patch, queue_task),
        patch("app.queue.tasks.backfill._append_backfill_item_event", item_audit),
        patch("app.queue.tasks.backfill._append_backfill_run_event", run_audit),
    ):
        await case.task(ctx=_ctx())

    assert queue_task.kiq.await_count == len(targets)
    assert _item_outcomes(item_audit) == [
        BackfillOutcomeCode.ITEM_ENQUEUED,
        BackfillOutcomeCode.ITEM_ENQUEUED,
    ]
    _assert_item_audit_derives_stage_from_backfill_stage(item_audit, case)
    # 成功 run では run event が一切焼かれない (occurrence は metric へ移設)。
    run_audit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
async def test_enqueue_failure_is_audited_and_later_items_continue(
    case: _TaskCase,
) -> None:
    """1 item の enqueue 失敗では task 全体を raise せず後続を続ける。"""
    targets = [_target(1), _target(2), _target(3)]
    backlog = MagicMock()
    setattr(backlog, case.target_method, AsyncMock(return_value=targets))
    setattr(backlog, case.count_method, AsyncMock(return_value=len(targets)))
    run_audit = AsyncMock()
    item_audit = AsyncMock()
    queue_task = SimpleNamespace(
        kiq=AsyncMock(side_effect=[None, RuntimeError("queue down"), None])
    )

    with (
        patch.object(tasks.settings, case.enabled_attr, True),
        patch(case.hold_patch, AsyncMock(return_value=False)),
        patch(case.ageout_patch, AsyncMock(return_value=0)),
        patch("app.queue.tasks.backfill.PipelineBacklog", return_value=backlog),
        patch(
            "app.queue.tasks.backfill.consume_daily_budget",
            AsyncMock(return_value=len(targets)),
        ),
        patch(case.queue_task_patch, queue_task),
        patch("app.queue.tasks.backfill._append_backfill_item_event", item_audit),
        patch("app.queue.tasks.backfill._append_backfill_run_event", run_audit),
    ):
        await case.task(ctx=_ctx())

    assert queue_task.kiq.await_count == len(targets)
    assert _item_outcomes(item_audit) == [
        BackfillOutcomeCode.ITEM_ENQUEUED,
        BackfillOutcomeCode.ITEM_ENQUEUE_FAILED,
        BackfillOutcomeCode.ITEM_ENQUEUED,
    ]
    _assert_item_audit_derives_stage_from_backfill_stage(item_audit, case)
    # item enqueue failed の forensic (exc) は残る (保証2)。
    failed_call = next(
        call
        for call in item_audit.await_args_list
        if call.kwargs["outcome_code"] == BackfillOutcomeCode.ITEM_ENQUEUE_FAILED
    )
    assert failed_call.kwargs["exc"] is not None
    # 成功 run では run summary を焼かない。
    run_audit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
async def test_selection_failure_is_audited_and_reraised(case: _TaskCase) -> None:
    """selection 例外は run failed として監査され、元例外を再 raise する。"""
    backlog = MagicMock()
    setattr(
        backlog,
        case.target_method,
        AsyncMock(side_effect=RuntimeError("select failed")),
    )
    setattr(backlog, case.count_method, AsyncMock(return_value=1))
    run_audit = AsyncMock()

    with (
        patch.object(tasks.settings, case.enabled_attr, True),
        patch(case.hold_patch, AsyncMock(return_value=False)),
        patch(case.ageout_patch, AsyncMock(return_value=0)),
        patch("app.queue.tasks.backfill.PipelineBacklog", return_value=backlog),
        patch("app.queue.tasks.backfill._append_backfill_run_event", run_audit),
    ):
        with pytest.raises(RuntimeError, match="select failed"):
            await case.task(ctx=_ctx())

    assert _run_outcomes(run_audit) == [BackfillOutcomeCode.RUN_FAILED]
    _assert_run_audit_derives_stage_from_backfill_stage(run_audit, case)
    assert run_audit.await_args.kwargs["exc"] is not None


# ---------------------------------------------------------------------------
# pipeline_stage span 配線テスト
# ---------------------------------------------------------------------------


def _backfill_ctx_with_targets(
    case: _TaskCase,
    targets: list[BackfillTarget],
) -> tuple[SimpleNamespace, MagicMock, MagicMock]:
    """backfill span テスト共通 setup。(ctx, backlog, queue_task) を返す。"""
    ctx = _ctx()
    backlog = MagicMock()
    setattr(backlog, case.target_method, AsyncMock(return_value=targets))
    setattr(backlog, case.count_method, AsyncMock(return_value=len(targets)))
    queue_task = SimpleNamespace(kiq=AsyncMock())
    return ctx, backlog, queue_task


class TestBackfillStageSpan:
    """backfill 3 task が pipeline_stage span を正しく開く配線テスト。

    kill switch 有効 + hold なし + targets 1 件 + budget あり の正常系を流し、
    pipeline_stage span がちょうど 1 件開くことを確認する。
    """

    @pytest.mark.asyncio
    async def test_backfill_curations_span_stage_and_op(
        self, capfire: CaptureLogfire
    ) -> None:
        """stage=backfill_curate / op=backfill_curations が span に開く。"""
        case = next(c for c in CASES if c.name == "curate")
        ctx, backlog, queue_task = _backfill_ctx_with_targets(case, [_target(1)])

        with (
            patch.object(tasks.settings, case.enabled_attr, True),
            patch(case.hold_patch, AsyncMock(return_value=False)),
            patch(case.ageout_patch, AsyncMock(return_value=0)),
            patch("app.queue.tasks.backfill.PipelineBacklog", return_value=backlog),
            patch(
                "app.queue.tasks.backfill.consume_daily_budget",
                AsyncMock(return_value=1),
            ),
            patch(case.queue_task_patch, queue_task),
            patch("app.queue.tasks.backfill._append_backfill_item_event", AsyncMock()),
            patch("app.queue.tasks.backfill._append_backfill_run_event", AsyncMock()),
        ):
            await tasks.backfill_curations(ctx=ctx)

        attrs = pipeline_stage_attrs(capfire)
        assert attrs["stage"] == Stage.BACKFILL_CURATE.value  # == "backfill_curate"
        assert attrs["op"] == "backfill_curations"

    @pytest.mark.asyncio
    async def test_backfill_assessments_span_stage_and_op(
        self, capfire: CaptureLogfire
    ) -> None:
        """stage=backfill_assess / op=backfill_assessments が span に開く。"""
        case = next(c for c in CASES if c.name == "assess")
        ctx, backlog, queue_task = _backfill_ctx_with_targets(case, [_target(1)])

        with (
            patch.object(tasks.settings, case.enabled_attr, True),
            patch(case.hold_patch, AsyncMock(return_value=False)),
            patch(case.ageout_patch, AsyncMock(return_value=0)),
            patch("app.queue.tasks.backfill.PipelineBacklog", return_value=backlog),
            patch(
                "app.queue.tasks.backfill.consume_daily_budget",
                AsyncMock(return_value=1),
            ),
            patch(case.queue_task_patch, queue_task),
            patch("app.queue.tasks.backfill._append_backfill_item_event", AsyncMock()),
            patch("app.queue.tasks.backfill._append_backfill_run_event", AsyncMock()),
        ):
            await tasks.backfill_assessments(ctx=ctx)

        attrs = pipeline_stage_attrs(capfire)
        assert attrs["stage"] == Stage.BACKFILL_ASSESS.value  # == "backfill_assess"
        assert attrs["op"] == "backfill_assessments"

    @pytest.mark.asyncio
    async def test_backfill_embeddings_span_stage_and_op(
        self, capfire: CaptureLogfire
    ) -> None:
        """stage=backfill_embed / op=backfill_embeddings が span に開く。"""
        case = next(c for c in CASES if c.name == "embed")
        ctx, backlog, queue_task = _backfill_ctx_with_targets(case, [_target(1)])

        with (
            patch.object(tasks.settings, case.enabled_attr, True),
            patch(case.hold_patch, AsyncMock(return_value=False)),
            patch(case.ageout_patch, AsyncMock(return_value=0)),
            patch("app.queue.tasks.backfill.PipelineBacklog", return_value=backlog),
            patch(
                "app.queue.tasks.backfill.consume_daily_budget",
                AsyncMock(return_value=1),
            ),
            patch(case.queue_task_patch, queue_task),
            patch("app.queue.tasks.backfill._append_backfill_item_event", AsyncMock()),
            patch("app.queue.tasks.backfill._append_backfill_run_event", AsyncMock()),
        ):
            await tasks.backfill_embeddings(ctx=ctx)

        attrs = pipeline_stage_attrs(capfire)
        assert attrs["stage"] == Stage.BACKFILL_EMBED.value  # == "backfill_embed"
        assert attrs["op"] == "backfill_embeddings"
