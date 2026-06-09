"""tier 別 dispatch の分岐配線テスト (unit)。

``dispatch_high`` / ``dispatch_medium`` / ``dispatch_low`` と全 tier 一括の
``dispatch_sources`` が、どの active source に ``acquire_source`` を kiq するかの
契約を固定する:

- cadence 指定の dispatch はその tier の source 定義のみ kiq する。
- DB の active source 名が ``SOURCES`` に無ければ (コード未登録) skip する。
- ``dispatch_sources`` (cadence=None) は登録済 active source を全て kiq する。

DB / Redis を持たず、session は fake、``SOURCES`` と ``acquire_source.kiq`` を
monkeypatch して配線だけを検証する。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from logfire.testing import CaptureLogfire

from app.collection.sources.fetch_cadence import FetchCadence
from app.collection.sources.source_name import SourceName
from app.queue.tasks import acquisition as collection_tasks

# fake registry: name → tier だけ持つ source 定義スタンド。
_FAKE_SOURCES = {
    SourceName("Alpha"): SimpleNamespace(fetch_cadence=FetchCadence.HIGH),
    SourceName("Beta"): SimpleNamespace(fetch_cadence=FetchCadence.MEDIUM),
    SourceName("Gamma"): SimpleNamespace(fetch_cadence=FetchCadence.LOW),
}


class _FakeResult:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def all(self) -> list[SimpleNamespace]:
        return self._rows


class _FakeSessionFactory:
    def __init__(
        self,
        rows: list[SimpleNamespace],
        *,
        execute_exc: BaseException | None = None,
    ) -> None:
        self.rows = rows
        self.execute_exc = execute_exc
        self.events: list[Any] = []
        self.commits = 0

    def __call__(self) -> _FakeSession:
        return _FakeSession(self)


class _FakeSession:
    def __init__(self, factory: _FakeSessionFactory) -> None:
        self._factory = factory

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def execute(self, _stmt: Any) -> _FakeResult:
        if self._factory.execute_exc is not None:
            raise self._factory.execute_exc
        return _FakeResult(self._factory.rows)

    def add(self, event: Any) -> None:
        self._factory.events.append(event)

    async def commit(self) -> None:
        self._factory.commits += 1


def _ctx(
    rows: list[SimpleNamespace],
    *,
    execute_exc: BaseException | None = None,
) -> SimpleNamespace:
    """``ctx.state.session_factory`` だけを持つ最小 mock。"""
    session_factory = _FakeSessionFactory(rows, execute_exc=execute_exc)
    return SimpleNamespace(state=SimpleNamespace(session_factory=session_factory))


def _row(source_id: int, name: str) -> SimpleNamespace:
    """``select(NewsSource.id, raw_name)`` の 1 行スタンド。"""
    return SimpleNamespace(id=source_id, name=name, raw_name=name)


def _events(ctx: SimpleNamespace) -> list[Any]:
    return ctx.state.session_factory.events


def _outcome_codes(ctx: SimpleNamespace) -> list[str]:
    return [event.outcome_code for event in _events(ctx)]


@pytest.fixture
def captured_kiq(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """``acquire_source.kiq`` と ``SOURCES`` を差し替え、kiq 呼び出しを捕捉する。"""
    kiq = AsyncMock()
    monkeypatch.setattr(collection_tasks, "acquire_source", SimpleNamespace(kiq=kiq))
    monkeypatch.setattr(
        "app.collection.article_acquisition.strategy.SOURCES", _FAKE_SOURCES
    )
    return kiq


def _kiqed_names(kiq: AsyncMock) -> list[str]:
    """kiq に渡された ``AcquireSourceArg`` の name 一覧 (呼び出し順)。"""
    return [call.args[0].name for call in kiq.call_args_list]


def _find_metric(metrics: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((m for m in metrics if m["name"] == name), None)


def _attributes_for(metric: dict[str, Any]) -> list[dict[str, Any]]:
    return [dp.get("attributes", {}) for dp in metric["data"]["data_points"]]


def _has_point(
    metric: dict[str, Any], *, value: int, attributes: dict[str, str]
) -> bool:
    return any(
        dp["value"] == value and dp.get("attributes") == attributes
        for dp in metric["data"]["data_points"]
    )


def _collected_metrics(capfire: CaptureLogfire) -> list[dict[str, Any]]:
    """metric 未記録時の capfire 例外を空 list として扱う (1 度だけ読む)。"""
    try:
        return capfire.get_collected_metrics()
    except AttributeError:
        return []


@pytest.mark.asyncio
async def test_dispatch_high_dispatches_only_high_tier(
    captured_kiq: AsyncMock,
) -> None:
    """``dispatch_high`` は HIGH tier の source のみ kiq する。"""
    rows = [_row(1, "Alpha"), _row(2, "Beta"), _row(3, "Gamma")]
    ctx = _ctx(rows)

    result = await collection_tasks.dispatch_high(ctx=ctx)  # type: ignore[arg-type]

    assert _kiqed_names(captured_kiq) == ["Alpha"]
    assert result == {"dispatched_count": 1}
    # 成功 occurrence は metric へ移設。監査は per-run heartbeat (件数なし) のみ。
    assert _outcome_codes(ctx) == ["dispatch_run_completed"]
    event = _events(ctx)[0]
    assert event.stage == "dispatch"
    assert event.event_type == "succeeded"
    assert event.source_id is None
    assert event.payload["cadence"] == "high"


@pytest.mark.asyncio
async def test_unregistered_db_source_is_skipped(
    captured_kiq: AsyncMock,
) -> None:
    """``SOURCES`` に無い active source 名は kiq されず skip される。"""
    rows = [_row(1, "Alpha"), _row(9, "Ghost")]
    ctx = _ctx(rows)

    result = await collection_tasks.dispatch_sources(ctx=ctx)  # type: ignore[arg-type]

    assert _kiqed_names(captured_kiq) == ["Alpha"]
    assert result == {"dispatched_count": 1}
    # rejection は which source + reason を監査に残し、成功は heartbeat に畳む。
    assert _outcome_codes(ctx) == ["source_not_registered", "dispatch_run_completed"]
    rejection, heartbeat = _events(ctx)
    assert rejection.event_type == "rejected"
    assert rejection.source_id == 9
    assert rejection.payload["source_name"] == "Ghost"
    assert rejection.payload["cadence"] == "all"
    assert heartbeat.outcome_code == "dispatch_run_completed"


@pytest.mark.asyncio
async def test_dispatch_sources_dispatches_all_registered_tiers(
    captured_kiq: AsyncMock,
) -> None:
    """``dispatch_sources`` (cadence=None) は登録済 active を tier 横断で全て kiq。"""
    rows = [_row(1, "Alpha"), _row(2, "Beta"), _row(3, "Gamma")]
    ctx = _ctx(rows)

    result = await collection_tasks.dispatch_sources(ctx=ctx)  # type: ignore[arg-type]

    assert _kiqed_names(captured_kiq) == ["Alpha", "Beta", "Gamma"]
    assert result == {"dispatched_count": 3}
    # 3 件の成功 occurrence を 1 件の per-run heartbeat に畳む。
    assert _outcome_codes(ctx) == ["dispatch_run_completed"]
    assert _events(ctx)[0].payload["cadence"] == "all"


@pytest.mark.asyncio
async def test_invalid_source_name_is_rejected_without_stopping_dispatch(
    captured_kiq: AsyncMock,
) -> None:
    """不正な source name は source 単位 rejected に畳み、他 source は継続。"""
    rows = [_row(1, "Alpha"), _row(8, "!!!"), _row(2, "Beta")]
    ctx = _ctx(rows)

    result = await collection_tasks.dispatch_sources(ctx=ctx)  # type: ignore[arg-type]

    assert _kiqed_names(captured_kiq) == ["Alpha", "Beta"]
    assert result == {"dispatched_count": 2}
    # 不正名の rejection は監査に残り、成功 2 件は heartbeat 1 行に畳む。
    assert _outcome_codes(ctx) == ["source_name_invalid", "dispatch_run_completed"]
    invalid = _events(ctx)[0]
    assert invalid.event_type == "rejected"
    assert invalid.source_id == 8
    assert invalid.error_class is not None
    assert invalid.payload["raw_source_name"] == "!!!"
    assert invalid.payload["cadence"] == "all"


@pytest.mark.asyncio
async def test_enqueue_failure_is_audited_and_later_sources_continue(
    captured_kiq: AsyncMock,
) -> None:
    """1 source の enqueue 失敗では task 全体を raise せず後続を続ける。"""
    rows = [_row(1, "Alpha"), _row(2, "Beta"), _row(3, "Gamma")]
    ctx = _ctx(rows)

    async def _kiq(arg: Any) -> None:
        if arg.name == "Beta":
            raise RuntimeError("queue down")

    captured_kiq.side_effect = _kiq

    result = await collection_tasks.dispatch_sources(ctx=ctx)  # type: ignore[arg-type]

    assert _kiqed_names(captured_kiq) == ["Alpha", "Beta", "Gamma"]
    assert result == {"dispatched_count": 2}
    # enqueue_failed は which source + error_* を監査に残し、成功は heartbeat に畳む。
    assert _outcome_codes(ctx) == ["source_enqueue_failed", "dispatch_run_completed"]
    failed = _events(ctx)[0]
    assert failed.event_type == "failed"
    assert failed.source_id == 2
    assert failed.error_class == "builtins.RuntimeError"
    assert failed.payload["error_message"] == "queue down"


@pytest.mark.asyncio
async def test_dispatch_no_targets_emits_run_metric_not_audit(
    captured_kiq: AsyncMock,
    capfire: CaptureLogfire,
) -> None:
    """対象0件の tick は監査に焼かず heartbeat metric で生死を表す。"""
    ctx = _ctx([])

    result = await collection_tasks.dispatch_sources(ctx=ctx)  # type: ignore[arg-type]

    assert captured_kiq.await_count == 0
    assert result == {"dispatched_count": 0}
    # 0 件は SUCCEEDED/FAILED 行を生まない (admin は行存在を読むため非対象)。
    assert _outcome_codes(ctx) == []
    run = _find_metric(_collected_metrics(capfire), "vector.dispatch.run")
    assert run is not None
    assert _has_point(
        run, value=1, attributes={"cadence": "all", "outcome": "no_targets"}
    )


@pytest.mark.asyncio
async def test_dispatched_occurrence_emitted_to_metric_not_audit(
    captured_kiq: AsyncMock,
    capfire: CaptureLogfire,
) -> None:
    """成功 occurrence は監査に焼かれず metric に出る (保証3)。"""
    rows = [_row(1, "Alpha"), _row(2, "Beta"), _row(3, "Gamma")]
    ctx = _ctx(rows)

    result = await collection_tasks.dispatch_sources(ctx=ctx)  # type: ignore[arg-type]

    assert result == {"dispatched_count": 3}
    # 監査に per-source 成功行は無く、per-run heartbeat 1 行のみ。
    assert "source_dispatched" not in _outcome_codes(ctx)
    assert _outcome_codes(ctx) == ["dispatch_run_completed"]
    # 成功 throughput は outcome metric に N=3、run は全件成功 succeeded。
    metrics = _collected_metrics(capfire)
    outcome = _find_metric(metrics, "vector.dispatch.outcome")
    assert outcome is not None
    assert _has_point(
        outcome,
        value=3,
        attributes={"cadence": "all", "result": "dispatched", "reason": "none"},
    )
    run = _find_metric(metrics, "vector.dispatch.run")
    assert run is not None
    assert _has_point(
        run, value=1, attributes={"cadence": "all", "outcome": "succeeded"}
    )


@pytest.mark.asyncio
async def test_partial_dispatch_records_partial_failed_and_failure_metric(
    captured_kiq: AsyncMock,
    capfire: CaptureLogfire,
) -> None:
    """一部 enqueue 失敗は run=partial_failed と成功/失敗件数を記録する。"""
    rows = [_row(1, "Alpha"), _row(2, "Beta"), _row(3, "Gamma")]
    ctx = _ctx(rows)

    async def _kiq(arg: Any) -> None:
        if arg.name == "Beta":
            raise RuntimeError("queue down")

    captured_kiq.side_effect = _kiq

    result = await collection_tasks.dispatch_sources(ctx=ctx)  # type: ignore[arg-type]

    assert result == {"dispatched_count": 2}
    # 1 件以上成功したので heartbeat 行は焼かれる。
    assert _outcome_codes(ctx) == ["source_enqueue_failed", "dispatch_run_completed"]
    metrics = _collected_metrics(capfire)
    run = _find_metric(metrics, "vector.dispatch.run")
    assert run is not None
    assert _has_point(
        run, value=1, attributes={"cadence": "all", "outcome": "partial_failed"}
    )
    outcome = _find_metric(metrics, "vector.dispatch.outcome")
    assert outcome is not None
    assert _has_point(
        outcome,
        value=2,
        attributes={"cadence": "all", "result": "dispatched", "reason": "none"},
    )
    assert _has_point(
        outcome,
        value=1,
        attributes={
            "cadence": "all",
            "result": "enqueue_failed",
            "reason": "unclassified",
        },
    )


@pytest.mark.asyncio
async def test_all_enqueue_fail_records_all_failed_and_no_heartbeat(
    captured_kiq: AsyncMock,
    capfire: CaptureLogfire,
) -> None:
    """全 enqueue 失敗は run=all_failed、heartbeat なし、失敗件数を記録する。"""
    rows = [_row(1, "Alpha"), _row(2, "Beta"), _row(3, "Gamma")]
    ctx = _ctx(rows)

    async def _kiq(arg: Any) -> None:
        raise RuntimeError("queue down")

    captured_kiq.side_effect = _kiq

    result = await collection_tasks.dispatch_sources(ctx=ctx)  # type: ignore[arg-type]

    assert result == {"dispatched_count": 0}
    # 全失敗 run は失敗行のみ、成功 heartbeat (dispatch_run_completed) は焼かない。
    assert "dispatch_run_completed" not in _outcome_codes(ctx)
    assert _outcome_codes(ctx) == ["source_enqueue_failed"] * 3
    metrics = _collected_metrics(capfire)
    run = _find_metric(metrics, "vector.dispatch.run")
    assert run is not None
    assert _has_point(
        run, value=1, attributes={"cadence": "all", "outcome": "all_failed"}
    )
    outcome = _find_metric(metrics, "vector.dispatch.outcome")
    assert outcome is not None
    assert _has_point(
        outcome,
        value=3,
        attributes={
            "cadence": "all",
            "result": "enqueue_failed",
            "reason": "unclassified",
        },
    )


@pytest.mark.asyncio
async def test_all_rejected_run_folds_to_no_targets(
    captured_kiq: AsyncMock,
    capfire: CaptureLogfire,
) -> None:
    """棄却 source のみの run は no_targets に畳み、棄却件数を記録する。"""
    ctx = _ctx([_row(9, "Ghost")])

    result = await collection_tasks.dispatch_sources(ctx=ctx)  # type: ignore[arg-type]

    assert captured_kiq.await_count == 0
    assert result == {"dispatched_count": 0}
    # 棄却は監査に残り、targets 空なので run は no_targets (heartbeat なし)。
    assert _outcome_codes(ctx) == ["source_not_registered"]
    metrics = _collected_metrics(capfire)
    run = _find_metric(metrics, "vector.dispatch.run")
    assert run is not None
    assert _has_point(
        run, value=1, attributes={"cadence": "all", "outcome": "no_targets"}
    )
    outcome = _find_metric(metrics, "vector.dispatch.outcome")
    assert outcome is not None
    assert _has_point(
        outcome,
        value=1,
        attributes={
            "cadence": "all",
            "result": "rejected",
            "reason": "source_not_registered",
        },
    )


@pytest.mark.asyncio
async def test_dispatch_metric_attributes_do_not_leak_pii(
    captured_kiq: AsyncMock,
    capfire: CaptureLogfire,
) -> None:
    """metric dump に source_id / source_name / 例外詳細を混ぜない (保証5)。"""
    rows = [_row(987654321, "Alpha"), _row(876543210, "Ghost")]
    ctx = _ctx(rows)

    async def _kiq(arg: Any) -> None:
        raise RuntimeError("secret-queue-failure")

    captured_kiq.side_effect = _kiq

    await collection_tasks.dispatch_sources(ctx=ctx)  # type: ignore[arg-type]

    metrics = _collected_metrics(capfire)
    dumped = json.dumps(metrics, default=str, ensure_ascii=False)
    for distinctive in (
        "987654321",
        "876543210",
        "Alpha",
        "Ghost",
        "secret-queue-failure",
    ):
        assert distinctive not in dumped

    expected_keys = {
        "vector.dispatch.outcome": {"cadence", "result", "reason"},
        "vector.dispatch.run": {"cadence", "outcome"},
    }
    for metric_name, keys in expected_keys.items():
        metric = _find_metric(metrics, metric_name)
        assert metric is not None
        for attrs in _attributes_for(metric):
            assert set(attrs.keys()) == keys


@pytest.mark.asyncio
async def test_dispatch_run_failed_is_audited_and_reraised(
    captured_kiq: AsyncMock,
    capfire: CaptureLogfire,
) -> None:
    """selection 自体の失敗は run failed を焼いて taskiq retry に渡す。"""
    ctx = _ctx([], execute_exc=RuntimeError("select failed"))

    with pytest.raises(RuntimeError, match="select failed"):
        await collection_tasks.dispatch_sources(ctx=ctx)  # type: ignore[arg-type]

    assert captured_kiq.await_count == 0
    # run crash の forensic は監査に残る (保証4)。
    assert _outcome_codes(ctx) == ["dispatch_run_failed"]
    event = _events(ctx)[0]
    assert event.event_type == "failed"
    assert event.error_class == "builtins.RuntimeError"
    assert event.payload["error_message"] == "select failed"
    assert event.payload["cadence"] == "all"
    run = _find_metric(_collected_metrics(capfire), "vector.dispatch.run")
    assert run is not None
    assert _has_point(
        run,
        value=1,
        attributes={"cadence": "all", "outcome": "target_selection_failed"},
    )
