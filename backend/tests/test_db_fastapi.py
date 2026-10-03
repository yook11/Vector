"""FastAPI DB依存がAPI lifecycleの資源を選ぶ契約。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import FastAPI, Request
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from starlette.types import Message, Receive, Scope, Send

import app.db.fastapi as db_fastapi
from app.main import app
from tests.conftest import TEST_ADMIN_ID, make_internal_jwt


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


@pytest.fixture
def entry_managed_api(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    """製品のSession依存を維持し、接続先だけをテストDBへ向ける。"""
    monkeypatch.setattr(app.state, "engine", db_session.bind, raising=False)
    return app


@pytest.mark.asyncio
async def test_entry_managed_api_commits_before_sending_success(
    entry_managed_api: FastAPI,
) -> None:
    """DBへのcommitが成功するまでHTTP成功応答を送信しない。"""
    events: list[str] = []

    def committed(_session: Session) -> None:
        events.append("committed")

    async def capture(scope: Scope, receive: Receive, send: Send) -> None:
        async def record_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                events.append(f"http_{message['status']}")
            await send(message)

        await entry_managed_api(scope, receive, record_send)

    event.listen(Session, "after_commit", committed)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=capture),
            base_url="http://test",
            headers={
                "Authorization": f"Bearer {make_internal_jwt(TEST_ADMIN_ID, 'admin')}",
            },
        ) as client:
            await client.post(
                "/api/v1/admin/sources",
                json={
                    "name": "Commit Order Source",
                    "sourceType": "rss",
                    "siteUrl": "https://example.com",
                    "endpointUrl": "https://example.com/feed",
                },
            )
    finally:
        event.remove(Session, "after_commit", committed)

    assert events == ["committed", "http_201"]


@pytest.mark.asyncio
async def test_entry_managed_api_returns_500_when_commit_fails(
    entry_managed_api: FastAPI,
) -> None:
    """commit失敗時は成功応答ではなく内部情報を含まない500を返す。"""
    commit_attempts: list[Session] = []

    def fail_commit(session: Session) -> None:
        commit_attempts.append(session)
        raise SQLAlchemyError("internal commit failure details")

    event.listen(Session, "before_commit", fail_commit)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=entry_managed_api, raise_app_exceptions=False),
            base_url="http://test",
            headers={
                "Authorization": f"Bearer {make_internal_jwt(TEST_ADMIN_ID, 'admin')}",
            },
        ) as client:
            response = await client.post(
                "/api/v1/admin/sources",
                json={
                    "name": "Failed Commit Source",
                    "sourceType": "rss",
                    "siteUrl": "https://example.com",
                    "endpointUrl": "https://example.com/feed",
                },
            )
    finally:
        event.remove(Session, "before_commit", fail_commit)

    assert len(commit_attempts) == 1
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
