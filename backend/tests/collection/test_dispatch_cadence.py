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

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

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
    assert _outcome_codes(ctx) == ["source_dispatched"]
    event = _events(ctx)[0]
    assert event.stage == "dispatch"
    assert event.event_type == "succeeded"
    assert event.source_id == 1
    assert event.payload["source_name"] == "Alpha"
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
    assert _outcome_codes(ctx) == ["source_not_registered", "source_dispatched"]
    rejection, dispatched = _events(ctx)
    assert rejection.event_type == "rejected"
    assert rejection.source_id == 9
    assert rejection.payload["source_name"] == "Ghost"
    assert rejection.payload["cadence"] == "all"
    assert dispatched.payload["source_name"] == "Alpha"


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
    assert _outcome_codes(ctx) == [
        "source_dispatched",
        "source_dispatched",
        "source_dispatched",
    ]
    assert {event.payload["cadence"] for event in _events(ctx)} == {"all"}


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
    assert _outcome_codes(ctx) == [
        "source_name_invalid",
        "source_dispatched",
        "source_dispatched",
    ]
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
    assert _outcome_codes(ctx) == [
        "source_dispatched",
        "source_enqueue_failed",
        "source_dispatched",
    ]
    failed = _events(ctx)[1]
    assert failed.event_type == "failed"
    assert failed.source_id == 2
    assert failed.error_class == "builtins.RuntimeError"
    assert failed.payload["error_message"] == "queue down"


@pytest.mark.asyncio
async def test_dispatch_no_targets_is_audited(
    captured_kiq: AsyncMock,
) -> None:
    """対象0件の tick は run 単位 skipped として監査する。"""
    ctx = _ctx([])

    result = await collection_tasks.dispatch_sources(ctx=ctx)  # type: ignore[arg-type]

    assert captured_kiq.await_count == 0
    assert result == {"dispatched_count": 0}
    assert _outcome_codes(ctx) == ["dispatch_run_no_targets"]
    event = _events(ctx)[0]
    assert event.event_type == "skipped"
    assert event.source_id is None
    assert event.payload["cadence"] == "all"
    assert event.payload["selected_count"] == 0
    assert event.payload["dispatched_count"] == 0


@pytest.mark.asyncio
async def test_dispatch_run_failed_is_audited_and_reraised(
    captured_kiq: AsyncMock,
) -> None:
    """selection 自体の失敗は run failed を焼いて taskiq retry に渡す。"""
    ctx = _ctx([], execute_exc=RuntimeError("select failed"))

    with pytest.raises(RuntimeError, match="select failed"):
        await collection_tasks.dispatch_sources(ctx=ctx)  # type: ignore[arg-type]

    assert captured_kiq.await_count == 0
    assert _outcome_codes(ctx) == ["dispatch_run_failed"]
    event = _events(ctx)[0]
    assert event.event_type == "failed"
    assert event.error_class == "builtins.RuntimeError"
    assert event.payload["error_message"] == "select failed"
    assert event.payload["cadence"] == "all"
