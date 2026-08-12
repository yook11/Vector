"""A1 供給ハートビート (EMF ``dispatch_run``) の emit 契約テスト。

``dispatch_high`` / ``dispatch_medium`` / ``dispatch_low`` は ``_dispatch`` の
**正常完了後**にのみ ``dispatch_run{cadence}`` の EMF 1 行を stdout に出す
(specs/observability/cloudwatch-alerting.md §A1/§2)。``_dispatch`` が例外を
上げた場合と、admin 手動経路の ``dispatch_sources`` は出さない。

dispatch の分岐配線 (どの source を kiq するか) 自体の正本は
``test_dispatch_cadence.py``。本ファイルは EMF emit の有無だけを検証するため、
対象 source 0 件で ``_dispatch`` を完走させる最小 fake session を使う。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.collection.sources.fetch_cadence import FetchCadence
from app.queue.tasks import acquisition as collection_tasks
from tests.cloudwatch.records import emf_records

_DISPATCH_RUN_METRIC = "dispatch_run"


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """active source 0 件を返す最小 fake。監査書き込みの add/commit は no-op。"""

    def __init__(self, execute_exc: BaseException | None) -> None:
        self._execute_exc = execute_exc

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def execute(self, _stmt: Any) -> _FakeResult:
        if self._execute_exc is not None:
            raise self._execute_exc
        return _FakeResult([])

    def add(self, _event: Any) -> None:
        return None

    async def commit(self) -> None:
        return None


def _ctx(*, execute_exc: BaseException | None = None) -> SimpleNamespace:
    """``ctx.state.session_factory`` だけを持つ最小 mock。"""

    def session_factory() -> _FakeSession:
        return _FakeSession(execute_exc)

    return SimpleNamespace(state=SimpleNamespace(session_factory=session_factory))


_CADENCE_TASKS = [
    (collection_tasks.dispatch_high, FetchCadence.HIGH),
    (collection_tasks.dispatch_medium, FetchCadence.MEDIUM),
    (collection_tasks.dispatch_low, FetchCadence.LOW),
]


@pytest.mark.parametrize(
    ("dispatch_task", "cadence"),
    _CADENCE_TASKS,
    ids=[cadence.value for _, cadence in _CADENCE_TASKS],
)
@pytest.mark.asyncio
async def test_dispatch_run_completion_emits_one_emf_line_for_cadence(
    dispatch_task: Any,
    cadence: FetchCadence,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """正常完了した cadence dispatch は ``dispatch_run{cadence}`` を 1 行出す。"""
    await dispatch_task(ctx=_ctx())

    emf_lines = emf_records(capsys.readouterr().out)

    assert len(emf_lines) == 1
    metric_def = emf_lines[0]["_aws"]["CloudWatchMetrics"][0]
    assert metric_def["Namespace"] == "Vector/Pipeline"
    assert metric_def["Metrics"] == [{"Name": _DISPATCH_RUN_METRIC, "Unit": "Count"}]
    assert metric_def["Dimensions"] == [["cadence"]]
    assert emf_lines[0][_DISPATCH_RUN_METRIC] == 1
    assert emf_lines[0]["cadence"] == cadence.value


@pytest.mark.parametrize(
    "dispatch_task",
    [task for task, _ in _CADENCE_TASKS],
    ids=[cadence.value for _, cadence in _CADENCE_TASKS],
)
@pytest.mark.asyncio
async def test_dispatch_run_failure_emits_no_emf_line(
    dispatch_task: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``_dispatch`` が例外を上げたら EMF を出さない (供給停止と誤認させない)。"""
    with pytest.raises(RuntimeError, match="select failed"):
        await dispatch_task(ctx=_ctx(execute_exc=RuntimeError("select failed")))

    assert emf_records(capsys.readouterr().out) == []


@pytest.mark.asyncio
async def test_dispatch_sources_manual_path_emits_no_emf_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """admin 手動経路の ``dispatch_sources`` は正常完了しても EMF を出さない。"""
    result = await collection_tasks.dispatch_sources(ctx=_ctx())

    assert result == {"dispatched_count": 0}
    assert emf_records(capsys.readouterr().out) == []
