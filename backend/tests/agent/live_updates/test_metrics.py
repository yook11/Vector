"""``app/agent/live_updates/metrics.py`` の emit 契約 oracle (正本)。

検証する不変条件:
- close は宣言された close reason すべてを attribute として通す (語彙の取りこぼしなし)
- close は件数 (connection_close) と滞留時間 (connection_duration) を同一 reason で
  同時に記録する。片方だけ落ちると切断分析が件数と時間で食い違う
- capacity rejection は宣言された scope すべてを通す
- open / close が active_connections を ±1 で釣り合わせる
- attribute は宣言語彙のみで、run_id / user_id 等の ID を載せない

sibling の ``answer_delta.breaker_open`` / ``execution_probe.unavailable`` は
``tests/agent/live_updates/test_answer_delta.py`` /
``tests/agent/runs/test_execution_probe.py`` が正本なので本ファイルでは扱わない。

capfire が ``logfire.configure`` を自前で呼ぶため setup_logfire は呼ばない。counter は
DELTA temporality で二度読みできないため、metric は 1 テスト 1 回だけ読む。
"""

from __future__ import annotations

from typing import get_args

import pytest
from logfire.testing import CaptureLogfire

from app.agent.live_updates.metrics import (
    AgentRunSseCapacityScope,
    AgentRunSseCloseReason,
    record_agent_run_sse_capacity_rejection,
    record_agent_run_sse_close,
    record_agent_run_sse_open,
    record_agent_run_sse_projection_drop,
)
from tests.logfire._metric_helpers import (
    assert_attribute_contract,
    collected_metrics,
)

_ACTIVE_METRIC = "vector.agent.sse.active_connections"
_CLOSE_METRIC = "vector.agent.sse.connection_close"
_DURATION_METRIC = "vector.agent.sse.connection_duration"
_CAPACITY_METRIC = "vector.agent.sse.capacity_rejection"
_PROJECTION_DROP_METRIC = "vector.agent.sse.projection_drop"

_ALL_CLOSE_REASONS = get_args(AgentRunSseCloseReason)
_ALL_CAPACITY_SCOPES = get_args(AgentRunSseCapacityScope)

# projection drop は allowlist 外イベントという単一原因しか持たないため
# production が固定値で emit する (metrics.py の record_agent_run_sse_projection_drop)。
_PROJECTION_DROP_REASON = "unknown_event"

_ANY_DURATION_SECONDS = 1.5


# close: 宣言語彙を全数流して attribute 契約を確かめる
# (値域 oracle は許可語彙を 1 値だけ流すと空虚になるため全数 parametrize する)


@pytest.mark.parametrize("reason", _ALL_CLOSE_REASONS)
def test_close_count_attributes_conform_for_every_declared_reason(
    capfire: CaptureLogfire, reason: str
) -> None:
    """connection_close の attribute は reason key のみ、値は宣言語彙内。"""
    record_agent_run_sse_close(
        duration_seconds=_ANY_DURATION_SECONDS,
        reason=reason,  # type: ignore[arg-type]
    )

    assert_attribute_contract(
        collected_metrics(capfire),
        _CLOSE_METRIC,
        allowed={"reason": _ALL_CLOSE_REASONS},
    )


@pytest.mark.parametrize("reason", _ALL_CLOSE_REASONS)
def test_close_duration_attributes_conform_for_every_declared_reason(
    capfire: CaptureLogfire, reason: str
) -> None:
    """connection_duration の attribute も reason key のみ、値は宣言語彙内。"""
    record_agent_run_sse_close(
        duration_seconds=_ANY_DURATION_SECONDS,
        reason=reason,  # type: ignore[arg-type]
    )

    assert_attribute_contract(
        collected_metrics(capfire),
        _DURATION_METRIC,
        allowed={"reason": _ALL_CLOSE_REASONS},
    )


def test_close_records_count_and_duration_together(capfire: CaptureLogfire) -> None:
    """1 回の close で件数と滞留時間が揃って出る (片肺で切断分析が食い違わない)。"""
    record_agent_run_sse_close(
        duration_seconds=_ANY_DURATION_SECONDS, reason="terminal"
    )

    emitted = {metric["name"] for metric in collected_metrics(capfire)}
    assert {_CLOSE_METRIC, _DURATION_METRIC} <= emitted


# capacity rejection: 宣言 scope を全数流す


@pytest.mark.parametrize("scope", _ALL_CAPACITY_SCOPES)
def test_capacity_rejection_attributes_conform_for_every_declared_scope(
    capfire: CaptureLogfire, scope: str
) -> None:
    """capacity_rejection の attribute は scope key のみ、値は宣言語彙内。"""
    record_agent_run_sse_capacity_rejection(scope=scope)  # type: ignore[arg-type]

    assert_attribute_contract(
        collected_metrics(capfire),
        _CAPACITY_METRIC,
        allowed={"scope": _ALL_CAPACITY_SCOPES},
    )


# projection drop: 単一原因の固定 reason


def test_projection_drop_attributes_conform_to_fixed_reason(
    capfire: CaptureLogfire,
) -> None:
    """projection_drop の attribute は固定 reason 1 値のみ。"""
    record_agent_run_sse_projection_drop()

    assert_attribute_contract(
        collected_metrics(capfire),
        _PROJECTION_DROP_METRIC,
        allowed={"reason": {_PROJECTION_DROP_REASON}},
    )


# active connections: 接続数の増減と、ID を載せない契約


def test_open_increments_active_connections(capfire: CaptureLogfire) -> None:
    """open 1 回で active_connections が +1。"""
    record_agent_run_sse_open()

    assert _sum_active(collected_metrics(capfire)) == 1


def test_close_decrements_active_connections(capfire: CaptureLogfire) -> None:
    """close 1 回で active_connections が -1 (open と釣り合う)。"""
    record_agent_run_sse_close(
        duration_seconds=_ANY_DURATION_SECONDS, reason="terminal"
    )

    assert _sum_active(collected_metrics(capfire)) == -1


def test_active_connections_carries_no_attributes(capfire: CaptureLogfire) -> None:
    """active_connections は attribute を持たない (run_id / user_id を載せない)。"""
    record_agent_run_sse_open()

    assert_attribute_contract(collected_metrics(capfire), _ACTIVE_METRIC, allowed={})


def _sum_active(metrics: list[dict[str, object]]) -> int:
    """active_connections gauge の全 data point 合計。"""
    metric = next((m for m in metrics if m["name"] == _ACTIVE_METRIC), None)
    assert metric is not None, f"{_ACTIVE_METRIC} が exporter に届かない"
    data = metric["data"]  # type: ignore[index]
    return sum(int(dp["value"]) for dp in data["data_points"])
