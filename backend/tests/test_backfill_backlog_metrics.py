"""``app/queue/tasks/backfill.py`` の ``vector.backfill.backlog`` gauge oracle。

PR #640 で撤去した circuit_breaker の観測代替として、``backfill_assessments``
cron tick 毎に DB 上の真の未処理件数 (LIMIT なし COUNT) を Logfire gauge で
set する設計を pin する。

検証する性質 (Phase 5-A):
- 真値が ``ASSESSMENTS_LIMIT`` を超えても gauge は saturate しない
  (= dispatch list の ``len(ids)`` でなく count メソッドの返値が使われる正本契約)
- 通常運転で gauge value = 真の count + attribute ``{"stage": "assessment"}``
- empty backlog でも ``set(0, ...)`` で baseline が記録される (gauge セマンティクス)
- kill switch off の時は SELECT 自体走らないため gauge も更新されない
  (= 「metric 不在 = kill switch off」のシグナル)
- attribute / value に固有 curation_id 等の dynamic 値が乗らない (capfire PII oracle)

設計スタンス:
- 実際の DB session_factory を立てるとテストが integration 寄りになる。本テストは
  unit 層で metric 経路のみ検証するため、``PipelineBacklog`` / ``consume_daily_budget``
  を patch 対象に絞り、metric record の attribute 契約を pin する
  (``feedback_test_invariants_over_change_tracking``)。
- capfire fixture が ``logfire.configure(...)`` を自前で呼ぶため本テスト内では
  ``setup_logfire`` を呼ばない (二重 configure 回避、Phase 4 慣習と同形)。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from logfire.testing import CaptureLogfire

from app.queue.helpers.backlog import BackfillTarget
from app.queue.tasks import backfill

# ---------------------------------------------------------------------------
# ヘルパー (Phase 4 の test_maintenance_age_delete_metrics.py と同形 —
# module 跨ぎで複製、共通化は「同じ問題」検出時に括る)
# ---------------------------------------------------------------------------


def _find_metric(metrics: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((m for m in metrics if m["name"] == name), None)


def _attributes_for(metric: dict[str, Any]) -> list[dict[str, Any]]:
    return [dp.get("attributes", {}) for dp in metric["data"]["data_points"]]


def _make_session_factory() -> Any:
    """session_factory(): async context manager を返す callable (AsyncSession stub)。"""

    class _AsyncSessionStub:
        async def __aenter__(self) -> _AsyncSessionStub:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    return MagicMock(side_effect=_AsyncSessionStub)


def _ctx_with_session_factory() -> MagicMock:
    """taskiq Context モック (session_factory が async context manager を返す)。"""
    ctx = MagicMock()
    ctx.state.session_factory = _make_session_factory()
    return ctx


def _target(curation_id: int) -> BackfillTarget:
    """assessment backfill target の test double を返す。"""
    return BackfillTarget(
        target_id=curation_id,
        article_id=curation_id + 1000,
        source_name="VentureBeat",
    )


# ---------------------------------------------------------------------------
# Test 1 (最重要 oracle): LIMIT を超えた真値が gauge に出る
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gauge_records_true_count_not_capped_by_limit(
    capfire: CaptureLogfire,
) -> None:
    """真の backlog が ASSESSMENTS_LIMIT を超えても gauge は真値を返す
    (= saturate しない構造的契約)。

    本テストは circuit_breaker 代替の観測機能を pin する正本 oracle。将来誰かが
    ``gauge.set(len(ids), ...)`` に戻す回帰を入れたら即落ちる
    (``feedback_per_seam_mapping_totality_oracle``)。
    """
    ctx = _ctx_with_session_factory()
    # dispatch list は LIMIT で頭打ち、真の backlog はそれを大きく超過
    fake_ids_at_limit = list(range(backfill.ASSESSMENTS_LIMIT))
    true_count = backfill.ASSESSMENTS_LIMIT * 4  # 200

    with (
        patch.object(backfill.settings, "backfill_assessments_enabled", True),
        patch(
            "app.queue.tasks.backfill.is_assessment_held",
            AsyncMock(return_value=False),
        ),
        patch(
            "app.queue.tasks.backfill._exclude_aged_out_assessments",
            AsyncMock(),
        ),
        patch("app.queue.tasks.backfill.PipelineBacklog") as backlog_cls,
        patch(
            "app.queue.tasks.backfill.consume_daily_budget",
            AsyncMock(return_value=0),  # daily_budget で early-return → kiq 不要
        ),
        patch("app.queue.tasks.backfill._append_backfill_run_event", AsyncMock()),
    ):
        backlog_cls.return_value.count_curations_pending_assessment = AsyncMock(
            return_value=true_count
        )
        backlog_cls.return_value.assessment_targets_pending = AsyncMock(
            return_value=[_target(curation_id) for curation_id in fake_ids_at_limit]
        )
        await backfill.backfill_assessments(ctx=ctx)

    metrics = capfire.get_collected_metrics()
    backlog_metric = _find_metric(metrics, "vector.backfill.backlog")
    assert backlog_metric is not None, "vector.backfill.backlog が記録されていない"
    points = backlog_metric["data"]["data_points"]
    assert any(
        dp["value"] == true_count and dp.get("attributes") == {"stage": "assessment"}
        for dp in points
    ), (
        f"gauge が LIMIT={len(fake_ids_at_limit)} で saturate した可能性 "
        f"(= 真値 {true_count} を出していない)。observed points: {points}"
    )


# ---------------------------------------------------------------------------
# Test 2: 通常運転 (LIMIT 以下) で count と stage attribute が出る
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gauge_records_count_with_stage_attribute(
    capfire: CaptureLogfire,
) -> None:
    """LIMIT 以下の通常運転で gauge value = count, attribute = {stage: assessment}。"""
    ctx = _ctx_with_session_factory()

    with (
        patch.object(backfill.settings, "backfill_assessments_enabled", True),
        patch(
            "app.queue.tasks.backfill.is_assessment_held",
            AsyncMock(return_value=False),
        ),
        patch(
            "app.queue.tasks.backfill._exclude_aged_out_assessments",
            AsyncMock(),
        ),
        patch("app.queue.tasks.backfill.PipelineBacklog") as backlog_cls,
        patch(
            "app.queue.tasks.backfill.consume_daily_budget",
            AsyncMock(return_value=0),
        ),
        patch("app.queue.tasks.backfill._append_backfill_run_event", AsyncMock()),
    ):
        backlog_cls.return_value.count_curations_pending_assessment = AsyncMock(
            return_value=3
        )
        backlog_cls.return_value.assessment_targets_pending = AsyncMock(
            return_value=[_target(101), _target(102), _target(103)]
        )
        await backfill.backfill_assessments(ctx=ctx)

    metrics = capfire.get_collected_metrics()
    backlog_metric = _find_metric(metrics, "vector.backfill.backlog")
    assert backlog_metric is not None
    points = backlog_metric["data"]["data_points"]
    assert any(
        dp["value"] == 3 and dp.get("attributes") == {"stage": "assessment"}
        for dp in points
    )


# ---------------------------------------------------------------------------
# Test 3: empty backlog でも set される (baseline 契約)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gauge_records_zero_when_empty_backlog(
    capfire: CaptureLogfire,
) -> None:
    """count=0 でも gauge.set(0, ...) が呼ばれ baseline が記録される。

    non-empty 時のみ set だと dashboard 上で最後の非ゼロ値が stale 表示される
    ため、empty も含めて常時 set する gauge セマンティクスを pin する。
    """
    ctx = _ctx_with_session_factory()

    with (
        patch.object(backfill.settings, "backfill_assessments_enabled", True),
        patch(
            "app.queue.tasks.backfill.is_assessment_held",
            AsyncMock(return_value=False),
        ),
        patch(
            "app.queue.tasks.backfill._exclude_aged_out_assessments",
            AsyncMock(),
        ),
        patch("app.queue.tasks.backfill.PipelineBacklog") as backlog_cls,
        patch("app.queue.tasks.backfill._append_backfill_run_event", AsyncMock()),
    ):
        backlog_cls.return_value.count_curations_pending_assessment = AsyncMock(
            return_value=0
        )
        backlog_cls.return_value.assessment_targets_pending = AsyncMock(return_value=[])
        # empty 経路は found==0 で早期 return するため consume_daily_budget は呼ばれない
        await backfill.backfill_assessments(ctx=ctx)

    metrics = capfire.get_collected_metrics()
    backlog_metric = _find_metric(metrics, "vector.backfill.backlog")
    assert backlog_metric is not None
    points = backlog_metric["data"]["data_points"]
    assert any(
        dp["value"] == 0 and dp.get("attributes") == {"stage": "assessment"}
        for dp in points
    )


# ---------------------------------------------------------------------------
# Test 4: kill switch off で SELECT 自体走らない → gauge も更新されない
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gauge_not_recorded_when_kill_switch_disabled(
    capfire: CaptureLogfire,
) -> None:
    """``backfill_assessments_enabled=False`` で gauge metric が一切記録されない。

    「metric が更新されていない期間 = kill switch off」を Logfire dashboard で
    読み取る運用シグナルを構造的に保証する。
    """
    ctx = _ctx_with_session_factory()

    with (
        patch.object(backfill.settings, "backfill_assessments_enabled", False),
        patch("app.queue.tasks.backfill.is_assessment_held", AsyncMock()) as held,
        patch("app.queue.tasks.backfill.PipelineBacklog") as backlog_cls,
        patch("app.queue.tasks.backfill._append_backfill_run_event", AsyncMock()),
    ):
        # 早期 return が起きれば PipelineBacklog 自体が触られないが、念のため設定
        backlog_cls.return_value.count_curations_pending_assessment = AsyncMock(
            return_value=42
        )
        await backfill.backfill_assessments(ctx=ctx)

    held.assert_not_called()
    # logfire.testing.get_collected_metrics は metric が 1 つも record されない
    # と内部 ``get_metrics_data()`` が ``None`` を返し AttributeError になる
    # (testing.py:125)。本テストの期待は「metric 不在」なのでこれを正常解釈する。
    try:
        metrics = capfire.get_collected_metrics()
    except AttributeError:
        metrics = []
    backlog_metric = _find_metric(metrics, "vector.backfill.backlog")
    assert backlog_metric is None, (
        f"kill switch off で gauge が記録された (期待: 不在): {backlog_metric}"
    )


# ---------------------------------------------------------------------------
# Test 5: PII 非含有契約 (capfire 全文検索 oracle)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gauge_attributes_do_not_leak_curation_ids(
    capfire: CaptureLogfire,
) -> None:
    """metric の attribute / dump 全体に固有 curation_id が乗らない。

    低 cardinality 契約 (attribute は ``stage`` のみ) を構造的に pin。将来
    ``attributes={"stage": ..., "curation_id": curation_id}`` のような変更が
    入った場合に発見する PII oracle。
    """
    distinctive_ids = [987654321, 876543210]  # 識別可能な大きな数値
    distinctive_count = 555444333  # count 値も dump に直接出ないことを確認

    ctx = _ctx_with_session_factory()

    with (
        patch.object(backfill.settings, "backfill_assessments_enabled", True),
        patch(
            "app.queue.tasks.backfill.is_assessment_held",
            AsyncMock(return_value=False),
        ),
        patch(
            "app.queue.tasks.backfill._exclude_aged_out_assessments",
            AsyncMock(),
        ),
        patch("app.queue.tasks.backfill.PipelineBacklog") as backlog_cls,
        patch(
            "app.queue.tasks.backfill.consume_daily_budget",
            AsyncMock(return_value=0),
        ),
        patch("app.queue.tasks.backfill._append_backfill_run_event", AsyncMock()),
    ):
        backlog_cls.return_value.count_curations_pending_assessment = AsyncMock(
            return_value=distinctive_count
        )
        backlog_cls.return_value.assessment_targets_pending = AsyncMock(
            return_value=[_target(curation_id) for curation_id in distinctive_ids]
        )
        await backfill.backfill_assessments(ctx=ctx)

    metrics = capfire.get_collected_metrics()
    backlog_metric = _find_metric(metrics, "vector.backfill.backlog")
    assert backlog_metric is not None

    # attribute は {"stage": "assessment"} のみ
    for attrs in _attributes_for(backlog_metric):
        assert set(attrs.keys()) == {"stage"}, (
            f"attribute に stage 以外の key が混入: {attrs}"
        )
        assert attrs["stage"] == "assessment"

    # dump 全文に curation_id が現れない (value は count なので含まれる、id は別)
    dumped = json.dumps(metrics, default=str, ensure_ascii=False)
    for curation_id in distinctive_ids:
        assert str(curation_id) not in dumped, (
            f"curation_id={curation_id} が metric dump に混入"
        )
