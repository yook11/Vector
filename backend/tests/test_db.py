"""Engine / Session 目録のレシピ契約。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.db.engine as db_engine
from app.db.engine import (
    API_POOL_MAX_OVERFLOW,
    API_POOL_SIZE,
    API_SERVICE_NAME,
    WORKER_POOL_RECYCLE_SECONDS,
    WORKER_POOL_SIZING,
    build_api_engine,
    build_cli_engine,
    build_worker_engine,
    worker_service_name,
)
from app.db.session import (
    caller_managed_session_factory,
    open_entry_managed_session,
)


def test_caller_managed_session_factory_disables_expire_on_commit() -> None:
    factory = caller_managed_session_factory(MagicMock())
    assert factory.class_ is AsyncSession
    assert factory.kw["expire_on_commit"] is False


class _Begin:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        self._session._in_transaction = True
        return self._session

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        self._session._in_transaction = False
        if exc_type is None:
            self._session.committed = True
        else:
            self._session.rolled_back = True


class _FakeSession:
    def __init__(
        self, bind: object = None, expire_on_commit: bool = True, **kwargs: object
    ) -> None:
        self.expire_on_commit = expire_on_commit
        self._in_transaction = False
        self.closed = False
        self.committed = False
        self.rolled_back = False

    def in_transaction(self) -> bool:
        return self._in_transaction

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True

    def begin(self) -> _Begin:
        return _Begin(self)


async def test_open_entry_managed_session_begins_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.db.session.AsyncSession", _FakeSession)
    async with open_entry_managed_session(MagicMock()) as session:
        assert session.expire_on_commit is True
        assert session.in_transaction()
    assert session.committed
    assert session.closed


async def test_open_entry_managed_session_rolls_back_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.db.session.AsyncSession", _FakeSession)
    with pytest.raises(RuntimeError, match="failed"):
        async with open_entry_managed_session(MagicMock()) as session:
            raise RuntimeError("failed")
    assert session.rolled_back
    assert session.closed


def test_build_api_engine_passes_only_usage_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = MagicMock()
    monkeypatch.setattr("app.db.engine.create_runtime_engine", spy)
    build_api_engine()
    spy.assert_called_once_with(
        application_name=API_SERVICE_NAME,
        pool_size=API_POOL_SIZE,
        max_overflow=API_POOL_MAX_OVERFLOW,
    )


def test_build_worker_engine_passes_only_usage_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = MagicMock()
    monkeypatch.setattr("app.db.engine.create_runtime_engine", spy)
    build_worker_engine("collection")
    pool_size, max_overflow = WORKER_POOL_SIZING["collection"]
    spy.assert_called_once_with(
        application_name=worker_service_name("collection"),
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_recycle=WORKER_POOL_RECYCLE_SECONDS,
    )


def test_build_cli_engine_passes_only_application_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = MagicMock()
    monkeypatch.setattr("app.db.engine.create_runtime_engine", spy)
    build_cli_engine("vector-cli-generate-briefing")
    spy.assert_called_once_with(application_name="vector-cli-generate-briefing")


def test_db_module_does_not_publish_engine() -> None:
    assert not hasattr(db_engine, "engine")
