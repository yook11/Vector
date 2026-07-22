from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from scripts import promote_admin as promote_script

_AUTH_DATABASE_URL = (
    "postgresql+asyncpg://vector_auth:auth-maintenance-secret@auth-db:5432/vector"
)
_APPLICATION_DATABASE_URL = (
    "postgresql+asyncpg://vector_app:application-secret@app-db:5432/vector"
)
_MIGRATION_DATABASE_URL = (
    "postgresql+asyncpg://vector:migration-secret@migration-db:5432/vector"
)
_DATABASE_URLS_WITH_SECRETS = (
    _AUTH_DATABASE_URL,
    _APPLICATION_DATABASE_URL,
    _MIGRATION_DATABASE_URL,
)


class _ConnectionContext:
    def __init__(
        self,
        connection: SimpleNamespace,
        enter_error: Exception | None = None,
    ) -> None:
        self._connection = connection
        self._enter_error = enter_error

    async def __aenter__(self) -> SimpleNamespace:
        if self._enter_error is not None:
            raise self._enter_error
        return self._connection

    async def __aexit__(self, *_: object) -> bool:
        return False


class _Engine:
    def __init__(
        self,
        connection: SimpleNamespace,
        enter_error: Exception | None = None,
    ) -> None:
        self.connect = MagicMock(
            return_value=_ConnectionContext(connection, enter_error)
        )
        self.dispose = AsyncMock()


class _ForbiddenApplicationEngine:
    def __init__(self) -> None:
        self.connect = MagicMock(
            side_effect=AssertionError(
                "DATABASE_URL engine must not be used by promote_admin"
            )
        )


def _connection(
    execute: AsyncMock | None = None,
    commit: AsyncMock | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        execute=execute or AsyncMock(),
        commit=commit or AsyncMock(),
    )


def _row_result(role: str | None) -> SimpleNamespace:
    row = None if role is None else SimpleNamespace(id="target-user", role=role)
    return SimpleNamespace(one_or_none=MagicMock(return_value=row))


def _patch_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    engine: _Engine,
    *,
    auth_database_url: str | None = _AUTH_DATABASE_URL,
) -> MagicMock:
    monkeypatch.setattr(
        promote_script,
        "settings",
        SimpleNamespace(
            auth_retention_database_url=auth_database_url,
            database_url=_APPLICATION_DATABASE_URL,
            migration_database_url=_MIGRATION_DATABASE_URL,
        ),
        raising=False,
    )
    factory = MagicMock(return_value=engine)
    monkeypatch.setattr(promote_script, "create_app_engine", factory, raising=False)
    monkeypatch.setattr(
        promote_script,
        "engine",
        _ForbiddenApplicationEngine(),
        raising=False,
    )
    return factory


@pytest.mark.asyncio
async def test_missing_auth_maintenance_url_exits_before_engine_or_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _connection()
    engine = _Engine(connection)
    factory = _patch_dependencies(monkeypatch, engine, auth_database_url=None)

    with pytest.raises(SystemExit) as exc_info:
        await promote_script.promote_or_demote("admin@example.com")

    assert exc_info.value.code != 0
    factory.assert_not_called()
    connection.execute.assert_not_awaited()
    engine.connect.assert_not_called()


@pytest.mark.asyncio
async def test_promote_uses_auth_maintenance_url_and_normalizes_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _connection(
        execute=AsyncMock(
            side_effect=[_row_result("user"), SimpleNamespace(rowcount=1)]
        )
    )
    engine = _Engine(connection)
    factory = _patch_dependencies(monkeypatch, engine)

    await promote_script.promote_or_demote("  ADMIN@EXAMPLE.COM  ")

    assert factory.call_args.args == (_AUTH_DATABASE_URL,)
    assert connection.execute.await_args_list[0].args[1] == {
        "email": "admin@example.com"
    }
    assert connection.execute.await_args_list[1].args[1]["role"] == "admin"
    connection.commit.assert_awaited_once()
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_user_exits_without_update_and_disposes_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _connection(execute=AsyncMock(return_value=_row_result(None)))
    engine = _Engine(connection)
    _patch_dependencies(monkeypatch, engine)

    with pytest.raises(SystemExit) as exc_info:
        await promote_script.promote_or_demote("missing@example.com")

    assert exc_info.value.code != 0
    assert connection.execute.await_count == 1
    connection.commit.assert_not_awaited()
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_target_already_has_requested_role_is_zero_exit_no_op_and_disposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _connection(execute=AsyncMock(return_value=_row_result("admin")))
    engine = _Engine(connection)
    _patch_dependencies(monkeypatch, engine)

    await promote_script.promote_or_demote("admin@example.com")

    assert connection.execute.await_count == 1
    connection.commit.assert_not_awaited()
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_demote_updates_only_the_existing_target_to_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _connection(
        execute=AsyncMock(
            side_effect=[_row_result("admin"), SimpleNamespace(rowcount=1)]
        )
    )
    engine = _Engine(connection)
    _patch_dependencies(monkeypatch, engine)

    await promote_script.promote_or_demote("admin@example.com", demote=True)

    assert connection.execute.await_args_list[1].args[1]["role"] == "user"
    connection.commit.assert_awaited_once()
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_commit_success_is_preserved_when_engine_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cleanup_secret = "dispose-secret-must-not-leak"
    connection = _connection(
        execute=AsyncMock(
            side_effect=[_row_result("user"), SimpleNamespace(rowcount=1)]
        )
    )
    engine = _Engine(connection)
    engine.dispose = AsyncMock(
        side_effect=SQLAlchemyError(
            f"cleanup failed {cleanup_secret} {_AUTH_DATABASE_URL}"
        )
    )
    _patch_dependencies(monkeypatch, engine)

    await promote_script.promote_or_demote("admin@example.com")

    captured = capsys.readouterr()
    assert captured.out == "Promoted user to role 'admin'.\n"
    assert captured.err == "Warning: database cleanup failed.\n"
    assert cleanup_secret not in captured.err
    assert all(url not in captured.err for url in _DATABASE_URLS_WITH_SECRETS)
    connection.commit.assert_awaited_once()
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ("connect", "query", "update", "commit"))
async def test_database_failures_exit_without_connection_secret_and_dispose_engine(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_phase: str,
) -> None:
    failure = SQLAlchemyError(
        "database failure for " + ", ".join(_DATABASE_URLS_WITH_SECRETS)
    )
    connection = _connection()
    enter_error = failure if failure_phase == "connect" else None
    if failure_phase == "query":
        connection.execute = AsyncMock(side_effect=failure)
    elif failure_phase == "update":
        connection.execute = AsyncMock(side_effect=[_row_result("user"), failure])
    elif failure_phase == "commit":
        connection.execute = AsyncMock(
            side_effect=[_row_result("user"), SimpleNamespace(rowcount=1)]
        )
        connection.commit = AsyncMock(side_effect=failure)
    engine = _Engine(connection, enter_error)
    _patch_dependencies(monkeypatch, engine)

    with pytest.raises(SystemExit) as exc_info:
        await promote_script.promote_or_demote("admin@example.com")

    captured = capsys.readouterr()
    rendered = f"{captured.out}{captured.err}{exc_info.value!s}{exc_info.value!r}"
    assert exc_info.value.code != 0
    assert all(url not in rendered for url in _DATABASE_URLS_WITH_SECRETS)
    engine.dispose.assert_awaited_once()
    if failure_phase != "commit":
        connection.commit.assert_not_awaited()
