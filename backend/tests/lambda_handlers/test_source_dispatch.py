"""予定回の入口での拒否・DB障害・実行資源の回収を、AWS・DBをモックにし実ロガーの出力で検証する。"""

import asyncio
import json
import sys
import threading
import traceback
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import OperationalError

from app.collection.sources.dispatch import SourceDispatchService
from app.lambda_handlers.source_dispatch import handler as entrypoint
from app.lambda_handlers.source_dispatch import resources
from app.lambda_handlers.source_dispatch.recorder import SourceDispatchRecorder
from app.lambda_handlers.source_dispatch.settings import SourceDispatchSettings
from app.log_policy.policies.pipeline_control import PIPELINE_CONTROL_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger

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


@pytest.fixture
def read_failed_logs(capsys):
    """標準出力のJSONログから、実行の失敗を記録した行だけを返す。"""

    def _read():
        output = capsys.readouterr().out
        logs = [json.loads(line) for line in output.splitlines()]
        return output, [log for log in logs if log["event"] == "source_dispatch_failed"]

    return _read


def test_invalid_schedule_stops_before_settings_and_resources(
    monkeypatch, read_failed_logs
):
    """不正な予定回は設定も資源も取得せず、入力値を含まない例外にする。"""
    settings = Mock()
    open_resources = Mock()
    monkeypatch.setattr(entrypoint, "SourceDispatchSettings", settings)
    monkeypatch.setattr(entrypoint, "open_source_dispatcher", open_resources)
    invalid_input = {**SCHEDULE, "private-input-value": "private-input-value"}
    with pytest.raises(RuntimeError) as caught:
        entrypoint.handler(invalid_input, None)
    output, failed = read_failed_logs()
    assert [log["operation"] for log in failed] == ["validate_event"]
    assert "private-input-value" not in "".join(
        traceback.format_exception(caught.value)
    )
    assert "private-input-value" not in output
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
    source_dispatch_env, monkeypatch, read_failed_logs, name, value
):
    """不足した接続設定やIAM条件違反では資源を取得せず、安全にエラー終了する。"""
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)
    open_resources = Mock()
    monkeypatch.setattr(entrypoint, "open_source_dispatcher", open_resources)
    with pytest.raises(RuntimeError) as caught:
        entrypoint.handler(SCHEDULE, None)
    output, failed = read_failed_logs()
    assert [log["operation"] for log in failed] == ["settings"]
    assert "private-password" not in "".join(traceback.format_exception(caught.value))
    assert "private-password" not in output
    open_resources.assert_not_called()


def test_database_failure_reaches_lambda_without_opening_sender(
    dispatch_resources, monkeypatch, read_failed_logs
):
    """対象選定のDB障害を正常結果に変換せず、送信を開始しない。"""
    _, session, _, _ = dispatch_resources
    error = OperationalError(None, None, RuntimeError("private-database-error"))
    monkeypatch.setattr(SourceDispatchService, "select", AsyncMock(side_effect=error))
    with pytest.raises(RuntimeError) as caught:
        entrypoint.handler(SCHEDULE, None)
    _, failed = read_failed_logs()
    assert [(log["operation"], log["error_class"]) for log in failed] == [
        ("dispatch", "sqlalchemy.exc.OperationalError")
    ]
    assert [call.args[0] for call in session.create_client.call_args_list] == ["rds"]
    assert "private-database-error" not in "".join(
        traceback.format_exception(caught.value)
    )


def test_resource_initialization_failure_closes_acquired_client(
    dispatch_resources, read_failed_logs
):
    """Engine準備で止まった場合にも、取得済みRDSクライアントを回収する。"""
    _, _, rds, _ = dispatch_resources
    resources.create_source_dispatch_engine.side_effect = RuntimeError(
        "private-init-error"
    )
    with pytest.raises(RuntimeError):
        entrypoint.handler(SCHEDULE, None)
    _, failed = read_failed_logs()
    assert [log["operation"] for log in failed] == ["resources"]
    rds.close.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_and_log_failure_preserve_success(
    dispatch_resources, monkeypatch
):
    """資源解放と診断の通常例外は、完了した処理を失敗に戻さない。"""
    settings, _, rds, engine = dispatch_resources
    engine.dispose.side_effect = RuntimeError("private-dispose-error")
    rds.close.side_effect = RuntimeError("private-close-error")

    class BrokenOutput:
        def write(self, text):
            raise OSError("private-log")

        def flush(self):
            raise OSError("private-log")

    monkeypatch.setattr(sys, "stdout", BrokenOutput())
    recorder = SourceDispatchRecorder(
        create_policy_json_logger("article_fetch", PIPELINE_CONTROL_LOG_RULES)
    )
    async with resources.open_source_dispatcher(settings, recorder=recorder):
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
    with pytest.raises(RuntimeError) as caught:
        async with resources.open_source_dispatcher(
            settings, recorder=SourceDispatchRecorder(Mock())
        ):
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
        async with resources.open_source_dispatcher(
            settings, recorder=SourceDispatchRecorder(Mock())
        ):
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
