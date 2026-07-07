"""``record_audit_dropped`` の e2e wiring tests。

各 drop site が実際にカウンタを emit することを、audit write を意図的に
失敗させて確認する。テスト内容:

- backfill item: ``backfill_stage="embed"`` から BACKFILL_EMBED が導出される。
- backfill run: ``backfill_stage="assess"`` から BACKFILL_ASSESS が導出される。
- dispatch run (stage はリテラル): ``_append_dispatch_run_event`` の
  except 分岐で Stage.DISPATCH が emit されることを確認。
- curation _audit_failure (stage はリテラル): ``CurationFailureHandler._audit_failure``
  の except 分岐で Stage.CURATION が emit されることを確認。

DB 不要: session_factory を常に例外を上げる double に差し替えて except 分岐を強制する。
capfire fixture が logfire.configure を自前で呼ぶため setup_logfire は不要。
"""

from __future__ import annotations

import pytest
from logfire.testing import CaptureLogfire

from app.analysis.curation.domain.ready import ReadyForCuration
from app.analysis.curation.errors import CurationRecoverableError
from app.analysis.curation.failure_handling import CurationFailureHandler
from app.audit.domain.event import EventType
from app.audit.stages.backfill import BackfillOutcomeCode
from app.audit.stages.dispatch import DispatchOutcomeCode
from app.queue.helpers.backlog import BackfillTarget
from app.queue.tasks.acquisition import _append_dispatch_run_event
from app.queue.tasks.backfill import (
    _append_backfill_item_event,
    _append_backfill_run_event,
)

_METRIC = "vector.audit.dropped"


class _FailingSessionFactory:
    """呼び出されると即 RuntimeError を上げる session factory test double。"""

    def __call__(self):  # noqa: ANN204
        return self

    async def __aenter__(self):  # noqa: ANN204
        raise RuntimeError("audit db down")

    async def __aexit__(self, *exc):  # noqa: ANN002
        return False


def _find_metric(metrics, name):  # noqa: ANN001, ANN202
    return next((m for m in metrics if m["name"] == name), None)


def _sum_value(metric):  # noqa: ANN001, ANN202
    return sum(int(dp["value"]) for dp in metric["data"]["data_points"])


def _attributes_for(metric):  # noqa: ANN001, ANN202
    return [dp.get("attributes", {}) for dp in metric["data"]["data_points"]]


# ---------------------------------------------------------------------------
# Site 1a: backfill item (stage は backfill_stage から導出)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_item_audit_drop_derives_stage_from_backfill_stage(
    capfire: CaptureLogfire,
) -> None:
    """audit write 失敗時に ``record_audit_dropped(stage)`` が +1 emit される。

    caller は event stage を渡さず、backfill_stage="embed" だけで
    wire 値 "backfill_embed" が attribute に乗ることを確認する。
    """
    target = BackfillTarget(
        target_id=1,
        analyzable_article_id=1001,
        source_name="TestSource",
    )

    await _append_backfill_item_event(
        _FailingSessionFactory(),
        backfill_stage="embed",
        run_id="run-wiring-001",
        target_kind="analyzed_article",
        target=target,
        event_type=EventType.SUCCEEDED,
        outcome_code=BackfillOutcomeCode.ITEM_ENQUEUED,
    )

    metric = _find_metric(capfire.get_collected_metrics(), _METRIC)
    assert metric is not None
    assert _sum_value(metric) == 1
    # wire 値 "backfill_embed" は Stage.BACKFILL_EMBED の StrEnum 値 (SSoT: event.py)。
    assert _attributes_for(metric) == [{"stage": "backfill_embed"}]


# ---------------------------------------------------------------------------
# Site 1b: backfill run (stage は backfill_stage から導出)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_run_audit_drop_derives_stage_from_backfill_stage(
    capfire: CaptureLogfire,
) -> None:
    """audit write 失敗時に ``record_audit_dropped(stage)`` が +1 emit される。

    caller は event stage を渡さず、backfill_stage="assess" だけで
    wire 値 "backfill_assess" が attribute に乗ることを確認する。
    """
    await _append_backfill_run_event(
        _FailingSessionFactory(),
        backfill_stage="assess",
        run_id="run-wiring-002",
        event_type=EventType.FAILED,
        outcome_code=BackfillOutcomeCode.RUN_FAILED,
    )

    metric = _find_metric(capfire.get_collected_metrics(), _METRIC)
    assert metric is not None
    assert _sum_value(metric) == 1
    # wire 値 "backfill_assess" は Stage.BACKFILL_ASSESS の StrEnum 値。
    assert _attributes_for(metric) == [{"stage": "backfill_assess"}]


# ---------------------------------------------------------------------------
# Site 2: dispatch run (stage はリテラル Stage.DISPATCH)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_run_audit_drop_increments_counter_with_dispatch_stage(
    capfire: CaptureLogfire,
) -> None:
    """audit write 失敗時に Stage.DISPATCH (wire="dispatch") が emit される。"""
    await _append_dispatch_run_event(
        _FailingSessionFactory(),
        event_type=EventType.FAILED,
        outcome_code=DispatchOutcomeCode.DISPATCH_RUN_FAILED,
        cadence="high",
        exc=RuntimeError("dispatch failed"),
    )

    metric = _find_metric(capfire.get_collected_metrics(), _METRIC)
    assert metric is not None
    assert _sum_value(metric) == 1
    # wire 値 "dispatch" は Stage.DISPATCH の StrEnum 値 (SSoT: event.py)。
    assert _attributes_for(metric) == [{"stage": "dispatch"}]


# ---------------------------------------------------------------------------
# Site 3: curation _audit_failure (stage はリテラル Stage.CURATION)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_curation_audit_failure_drop_increments_counter_with_curation_stage(
    capfire: CaptureLogfire,
) -> None:
    """audit write 失敗時に Stage.CURATION (wire="curation") が emit される。"""
    ready = ReadyForCuration(
        analyzable_article_id=42,
        original_title="Test Title",
        original_content="x" * 50,
    )
    exc = CurationRecoverableError(
        code="extraction_response_invalid",
        failure_kind="ai_response_invalid",
    )

    handler = CurationFailureHandler(_FailingSessionFactory())  # type: ignore[arg-type]
    # _audit_failure はプロトコル上 BaseCurator が必要だが、None を渡しても
    # session_factory が先に例外を上げるため curator の参照は到達しない。
    await handler._audit_failure(ready, exc, None)  # type: ignore[arg-type]

    metric = _find_metric(capfire.get_collected_metrics(), _METRIC)
    assert metric is not None
    assert _sum_value(metric) == 1
    # wire 値 "curation" は Stage.CURATION の StrEnum 値 (SSoT: event.py)。
    assert _attributes_for(metric) == [{"stage": "curation"}]
