"""agent run の SSE 配信が Logfire に残す観測記録の契約 (正本)。

運用者は Logfire だけを見て「今何本繋がっているか」「なぜ切れたか」「どの上限で
断られたか」を答える (SSE metric を読む CloudWatch alarm は無い)。本ファイルはその
答えが信用できる状態を固定する。

対象は 2 つのシーム:

1. recorder が渡された値を attribute としてどう載せるか
2. 接続の一巡 (取得 → 配信開始 → 解放) で active_connections が釣り合うか

「どの状況でどの close reason を渡すか」は ``test_sse.py`` が recorder を patch して
所有しているため、ここでは扱わない。capacity の上限判定そのもの (拒否時に返る enum) も
``test_sse.py`` の所有。

capfire が ``logfire.configure`` を自前で呼ぶため setup_logfire は呼ばない。counter は
DELTA temporality で二度読みできないため、metric は 1 テスト 1 回だけ読む。
"""

from __future__ import annotations

from typing import Any, get_args
from uuid import uuid4

import pytest
from logfire.testing import CaptureLogfire

from app.agent.live_updates.metrics import (
    AgentRunSseCapacityScope,
    AgentRunSseCloseReason,
    record_agent_run_sse_capacity_rejection,
    record_agent_run_sse_close,
    record_agent_run_sse_projection_drop,
)
from app.agent.live_updates.sse import AgentRunSseCapacity
from tests.logfire._metric_helpers import attributes_of, collected_metrics

_ACTIVE_METRIC = "vector.agent.sse.active_connections"
_CLOSE_METRIC = "vector.agent.sse.connection_close"
_DURATION_METRIC = "vector.agent.sse.connection_duration"
_CAPACITY_METRIC = "vector.agent.sse.capacity_rejection"
_PROJECTION_DROP_METRIC = "vector.agent.sse.projection_drop"

_ALL_CLOSE_REASONS = get_args(AgentRunSseCloseReason)
_ALL_CAPACITY_SCOPES = get_args(AgentRunSseCapacityScope)

_ANY_DURATION_SECONDS = 1.5


def _sum_values(metrics: list[dict[str, Any]], name: str) -> int:
    """``name`` metric の全 data point 合計。未収集は期待が崩れるため fail する。"""
    metric = next((m for m in metrics if m["name"] == name), None)
    assert metric is not None, f"{name} が exporter に届かない"
    return sum(int(point["value"]) for point in metric["data"]["data_points"])


# 切断の記録 — 「なぜ切れたか」と「どのくらい繋がっていたか」を突き合わせられること


def test_close_emits_count_and_duration_with_the_same_reason(
    capfire: CaptureLogfire,
) -> None:
    """1 回の切断が件数と滞留時間を同じ reason で残す。

    片方だけ落ちると reason 別の平均接続時間が出せなくなる。
    """
    reason = "client_disconnect"

    record_agent_run_sse_close(
        duration_seconds=_ANY_DURATION_SECONDS,
        reason=reason,
    )

    metrics = collected_metrics(capfire)
    assert attributes_of(metrics, _CLOSE_METRIC) == {"reason": reason}
    assert attributes_of(metrics, _DURATION_METRIC) == {"reason": reason}


@pytest.mark.parametrize("reason", _ALL_CLOSE_REASONS)
def test_every_declared_close_reason_reaches_the_metric(
    capfire: CaptureLogfire, reason: str
) -> None:
    """宣言済みの切断理由はすべて Logfire 側の reason として現れる。

    理由を足したのに metric に出ず、切断の内訳が読めない状態を防ぐ。
    """
    record_agent_run_sse_close(
        duration_seconds=_ANY_DURATION_SECONDS,
        reason=reason,  # type: ignore[arg-type]
    )

    assert attributes_of(collected_metrics(capfire), _CLOSE_METRIC) == {
        "reason": reason
    }


# 受け入れ拒否の記録 — 「どの上限に当たったか」が分かること


@pytest.mark.parametrize("scope", _ALL_CAPACITY_SCOPES)
def test_capacity_rejection_reports_which_limit_was_hit(
    capfire: CaptureLogfire, scope: str
) -> None:
    """接続を断ったときは run / user / process のどの上限かが残る。"""
    record_agent_run_sse_capacity_rejection(scope=scope)  # type: ignore[arg-type]

    assert attributes_of(collected_metrics(capfire), _CAPACITY_METRIC) == {
        "scope": scope
    }


# 配信漏れの記録 — 原因が allowlist 1 つに限られること


def test_projection_drop_reports_the_allowlist_as_the_only_cause(
    capfire: CaptureLogfire,
) -> None:
    """公開 allowlist で落としたイベントは単一原因として残る。

    reason 値は production が固定で渡す wire 値 (metrics.py が SSoT)。
    """
    record_agent_run_sse_projection_drop()

    assert attributes_of(collected_metrics(capfire), _PROJECTION_DROP_METRIC) == {
        "reason": "unknown_event"
    }


# 接続数の釣り合い — active_connections が「今何本繋がっているか」を答えられること


async def _acquire_streaming_lease(capacity: AgentRunSseCapacity):
    """配信開始まで進んだ lease を返す (取得 → 所有権 → 配信開始)。"""
    lease = await capacity.try_acquire_process()
    assert lease is not None
    assert await lease.try_acquire_owned(run_id=uuid4(), user_id=uuid4()) is None
    await lease.mark_stream_started()
    return lease


@pytest.mark.asyncio
async def test_completed_stream_returns_active_connections_to_zero(
    capfire: CaptureLogfire,
) -> None:
    """接続の一巡で active_connections が 0 に戻る。

    解放が漏れると値が単調増加し、接続数として読めなくなる。
    """
    capacity = AgentRunSseCapacity()
    lease = await _acquire_streaming_lease(capacity)
    lease.mark_close_reason("terminal")

    await lease.release()

    assert _sum_values(collected_metrics(capfire), _ACTIVE_METRIC) == 0


@pytest.mark.asyncio
async def test_lease_released_before_stream_start_records_no_connection(
    capfire: CaptureLogfire,
) -> None:
    """配信が始まらずに終わった lease は接続として数えない。

    上限確保だけして流さずに終わる経路 (前提未充足 / 早期エラー) を接続数に混ぜない。
    """
    capacity = AgentRunSseCapacity()
    lease = await capacity.try_acquire_process()
    assert lease is not None
    assert await lease.try_acquire_owned(run_id=uuid4(), user_id=uuid4()) is None

    await lease.release()

    emitted = {metric["name"] for metric in collected_metrics(capfire)}
    assert {_ACTIVE_METRIC, _CLOSE_METRIC}.isdisjoint(emitted)


@pytest.mark.asyncio
async def test_double_release_records_a_single_close(
    capfire: CaptureLogfire,
) -> None:
    """二重解放でも切断は 1 回しか記録されない。

    TTL 期限切れの掃除と明示解放が競合しても切断件数が水増しされない。
    """
    capacity = AgentRunSseCapacity()
    lease = await _acquire_streaming_lease(capacity)
    lease.mark_close_reason("terminal")

    await lease.release()
    await lease.release()

    assert _sum_values(collected_metrics(capfire), _CLOSE_METRIC) == 1
