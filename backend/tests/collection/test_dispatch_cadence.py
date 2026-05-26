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


class _FakeSession:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def execute(self, _stmt: Any) -> _FakeResult:
        return _FakeResult(self._rows)


def _ctx(rows: list[SimpleNamespace]) -> SimpleNamespace:
    """``ctx.state.session_factory`` だけを持つ最小 mock。"""
    return SimpleNamespace(
        state=SimpleNamespace(session_factory=lambda: _FakeSession(rows))
    )


def _row(source_id: int, name: str) -> SimpleNamespace:
    """``select(NewsSource.id, NewsSource.name)`` の 1 行スタンド。"""
    return SimpleNamespace(id=source_id, name=name)


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

    result = await collection_tasks.dispatch_high(ctx=_ctx(rows))  # type: ignore[arg-type]

    assert _kiqed_names(captured_kiq) == ["Alpha"]
    assert result == {"dispatched_count": 1}


@pytest.mark.asyncio
async def test_unregistered_db_source_is_skipped(
    captured_kiq: AsyncMock,
) -> None:
    """``SOURCES`` に無い active source 名は kiq されず skip される。"""
    rows = [_row(1, "Alpha"), _row(9, "Ghost")]

    result = await collection_tasks.dispatch_sources(ctx=_ctx(rows))  # type: ignore[arg-type]

    assert _kiqed_names(captured_kiq) == ["Alpha"]
    assert result == {"dispatched_count": 1}


@pytest.mark.asyncio
async def test_dispatch_sources_dispatches_all_registered_tiers(
    captured_kiq: AsyncMock,
) -> None:
    """``dispatch_sources`` (cadence=None) は登録済 active を tier 横断で全て kiq。"""
    rows = [_row(1, "Alpha"), _row(2, "Beta"), _row(3, "Gamma")]

    result = await collection_tasks.dispatch_sources(ctx=_ctx(rows))  # type: ignore[arg-type]

    assert _kiqed_names(captured_kiq) == ["Alpha", "Beta", "Gamma"]
    assert result == {"dispatched_count": 3}
