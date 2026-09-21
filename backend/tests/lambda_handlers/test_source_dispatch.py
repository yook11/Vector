"""予定回の入口での拒否・DB障害・実行資源の回収を検証する。"""

import asyncio
import threading
import traceback
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import OperationalError
from structlog.testing import capture_logs

from app.collection.sources.dispatch import SourceDispatchService
from app.lambda_handlers.source_dispatch import failure, resources
from app.lambda_handlers.source_dispatch import handler as entrypoint
from app.lambda_handlers.source_dispatch.failure import SourceDispatchLambdaError
from app.lambda_handlers.source_dispatch.settings import SourceDispatchSettings

SCHEDULE = {"cadence": "high", "scheduled_at": "2026-09-13T01:00:00Z"}


@pytest.fixture
def source_dispatch_env(monkeypatch):
    for name, value in {
        "ENV": "test",
        "DATABASE_URL": "postgresql+asyncpg://vector_collect@database.invalid/vector",
        "DB_IAM_AUTH": "true",
        "AWS_REGION": "ap-northeast-1",
        "SQS_SOURCE_ACQUISITION_QUEUE_URL": "https://sqs.invalid/acquisition",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(entrypoint, "setup_lambda_logging", lambda: None)


@pytest.fixture
def dispatch_resources(source_dispatch_env, monkeypatch):
    rds = Mock()
    session = Mock(create_client=Mock(return_value=rds))
    engine = Mock(dispose=AsyncMock())
    monkeypatch.setattr(resources, "Session", Mock(return_value=session))
    monkeypatch.setattr(
        resources, "create_source_dispatch_engine", Mock(return_value=engine)
    )
    return SourceDispatchSettings(), session, rds, engine


def test_invalid_schedule_stops_before_settings_and_resources(monkeypatch):
    """不正な予定回は設定も資源も取得せず、入力値を含まない例外にする。"""
    settings = Mock()
    open_resources = Mock()
    monkeypatch.setattr(entrypoint, "SourceDispatchSettings", settings)
    monkeypatch.setattr(entrypoint, "open_source_dispatcher", open_resources)
    monkeypatch.setattr(entrypoint, "setup_lambda_logging", lambda: None)
    invalid_input = {**SCHEDULE, "private-input-value": "private-input-value"}
    with capture_logs() as logs, pytest.raises(SourceDispatchLambdaError) as caught:
        entrypoint.handler(invalid_input, None)
    assert caught.value.phase == "input"
    assert "private-input-value" not in "".join(
        traceback.format_exception(caught.value)
    )
    assert "private-input-value" not in repr(logs)
    settings.assert_not_called()
    open_resources.assert_not_called()


@pytest.mark.parametrize(
    "name, value",
    [
        pytest.param("SQS_SOURCE_ACQUISITION_QUEUE_URL", None, id="missing-queue"),
        pytest.param("DB_IAM_AUTH", "false", id="iam-disabled"),
        pytest.param(
            "DATABASE_URL",
            "postgresql+asyncpg://vector_collect:private-password@database.invalid/vector",
            id="password-with-iam",
        ),
    ],
)
def test_invalid_settings_stop_before_opening_resources(
    source_dispatch_env, monkeypatch, name, value
):
    """不足した接続設定やIAM条件違反では資源を取得せず、安全にエラー終了する。"""
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)
    open_resources = Mock()
    monkeypatch.setattr(entrypoint, "open_source_dispatcher", open_resources)
    with capture_logs() as logs, pytest.raises(SourceDispatchLambdaError) as caught:
        entrypoint.handler(SCHEDULE, None)
    assert caught.value.phase == "settings"
    assert "private-password" not in "".join(traceback.format_exception(caught.value))
    assert "private-password" not in repr(logs)
    open_resources.assert_not_called()


def test_database_failure_reaches_lambda_without_opening_sender(
    dispatch_resources, monkeypatch
):
    """対象選定のDB障害を正常結果に変換せず、送信を開始しない。"""
    _, session, _, _ = dispatch_resources
    error = OperationalError(None, None, RuntimeError("private-database-error"))
    monkeypatch.setattr(SourceDispatchService, "select", AsyncMock(side_effect=error))
    with capture_logs() as logs, pytest.raises(SourceDispatchLambdaError) as caught:
        entrypoint.handler(SCHEDULE, None)
    assert caught.value.phase == "dispatch"
    assert caught.value.error_class == "sqlalchemy.exc.OperationalError"
    assert [call.args[0] for call in session.create_client.call_args_list] == ["rds"]
    assert "private-database-error" not in repr(logs)
    assert "private-database-error" not in "".join(
        traceback.format_exception(caught.value)
    )


def test_resource_initialization_failure_closes_acquired_client(dispatch_resources):
    """Engine準備で止まった場合にも、取得済みRDSクライアントを回収する。"""
    _, _, rds, _ = dispatch_resources
    resources.create_source_dispatch_engine.side_effect = RuntimeError(
        "private-init-error"
    )
    with capture_logs(), pytest.raises(SourceDispatchLambdaError) as caught:
        entrypoint.handler(SCHEDULE, None)
    assert caught.value.phase == "resources"
    rds.close.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_and_log_failure_preserve_success(
    dispatch_resources, monkeypatch
):
    """資源解放と診断の通常例外は、完了した処理を失敗に戻さない。"""
    settings, _, rds, engine = dispatch_resources
    engine.dispose.side_effect = RuntimeError("private-dispose-error")
    rds.close.side_effect = RuntimeError("private-close-error")
    monkeypatch.setattr(failure, "logger", Mock(warning=Mock(side_effect=RuntimeError)))
    async with resources.open_source_dispatcher(settings):
        pass
    engine.dispose.assert_awaited_once()
    rds.close.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_failure_preserves_original_failure(dispatch_resources):
    """処理中の失敗を、後続のEngine・RDS解放失敗で上書きしない。"""
    settings, _, rds, engine = dispatch_resources
    original = RuntimeError("original-failure")
    engine.dispose.side_effect = RuntimeError("dispose-failure")
    rds.close.side_effect = RuntimeError("close-failure")
    with capture_logs(), pytest.raises(RuntimeError) as caught:
        async with resources.open_source_dispatcher(settings):
            raise original
    assert caught.value is original
    engine.dispose.assert_awaited_once()
    rds.close.assert_called_once()


@pytest.mark.asyncio
async def test_cancellation_during_initialization_recovers_rds_client(
    dispatch_resources,
):
    """初期化中にキャンセルされても、作成が終わったRDSクライアントを回収する。"""
    settings, session, rds, engine = dispatch_resources
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def create_client(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return rds

    session.create_client.side_effect = create_client

    async def run():
        async with resources.open_source_dispatcher(settings):
            pytest.fail("cancelled initialization must not start dispatch")

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    rds.close.assert_called_once()
    engine.dispose.assert_not_awaited()


def test_source_dispatch_error_directly_inherits_exception():
    """Lambdaの投入例外は通常のExceptionを直接継承する。"""
    assert SourceDispatchLambdaError.__bases__ == (Exception,)


def test_source_dispatch_error_has_no_implicit_message():
    """処理段階と原因型を例外文面へ自動補完しない。"""
    error = SourceDispatchLambdaError(
        phase="dispatch", error_class="builtins.RuntimeError"
    )
    assert error.args == ()
    assert str(error) == ""
