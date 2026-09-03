"""FastAPI DB依存がAPI lifecycleの資源を選ぶ契約。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import Request

import app.db.fastapi as db_fastapi


async def test_entry_managed_dependency_uses_api_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = object()
    session = object()
    received: list[object] = []

    @asynccontextmanager
    async def open_session(selected_engine: object) -> AsyncIterator[object]:
        received.append(selected_engine)
        yield session

    monkeypatch.setattr(db_fastapi, "open_entry_managed_session", open_session)
    request = cast(
        Request,
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=engine))),
    )
    dependency = db_fastapi.get_entry_managed_session(request)

    assert await anext(dependency) is session
    with pytest.raises(StopAsyncIteration):
        await anext(dependency)
    assert received == [engine]


async def test_caller_managed_dependency_uses_api_factory() -> None:
    session = object()
    calls = 0

    @asynccontextmanager
    async def open_session() -> AsyncIterator[object]:
        nonlocal calls
        calls += 1
        yield session

    request = cast(
        Request,
        SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(session_factory=open_session),
            )
        ),
    )
    dependency = db_fastapi.get_caller_managed_session(request)

    assert await anext(dependency) is session
    with pytest.raises(StopAsyncIteration):
        await anext(dependency)
    assert calls == 1
