"""配送先に依存しないrelayの組み立て・実行・終了を確認する。"""

import asyncio
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.lambda_handlers.outbox_relay.settings import OutboxRelayConnectionSettings
from app.outbox.publishing.route import EventDeliveryRoute

execution = import_module("app.lambda_handlers.outbox_relay.execution")

pytestmark = pytest.mark.unit


@pytest.fixture
def execution_dependencies(monkeypatch):
    settings = OutboxRelayConnectionSettings(
        env="test",
        database_url="postgresql+asyncpg://user@database.invalid/vector",
        db_iam_auth=False,
        aws_region="ap-northeast-1",
    )
    route = EventDeliveryRoute("test.event", "https://sqs.invalid/test", Mock())
    trace = []
    engine = Mock(dispose=AsyncMock(side_effect=lambda: trace.append("disposed")))
    engine.connect.return_value = AsyncMock()
    create_engine = Mock(return_value=engine)
    factory = Mock()
    create_factory = Mock(return_value=factory)
    publisher = Mock()
    create_publisher = Mock(return_value=publisher)
    failure_handler = Mock()
    create_failure_handler = Mock(return_value=failure_handler)
    relay = Mock(run_once=AsyncMock(side_effect=lambda: trace.append("run_once")))
    create_relay = Mock(return_value=relay)
    monkeypatch.setattr(execution, "create_lambda_engine", create_engine)
    monkeypatch.setattr(execution, "caller_managed_session_factory", create_factory)
    monkeypatch.setattr(execution, "SqsSender", Mock(from_session=create_publisher))
    monkeypatch.setattr(
        execution, "OutboxDeliveryFailureHandler", create_failure_handler
    )
    monkeypatch.setattr(execution, "OutboxRelay", create_relay)
    return SimpleNamespace(**locals())


def test_passes_delivery_settings_to_publisher(execution_dependencies):
    """注入された配送定義とregionを送信・取得の組み立てに使う。"""
    asyncio.run(
        execution.run_relay(
            execution_dependencies.settings, execution_dependencies.route
        )
    )
    execution_dependencies.create_publisher.assert_called_once()
    kwargs = execution_dependencies.create_publisher.call_args.kwargs
    assert kwargs["region"] == execution_dependencies.settings.aws_region
    relay_call = execution_dependencies.create_relay.call_args
    publisher = relay_call.args[1]
    assert publisher._route is execution_dependencies.route
    assert relay_call.kwargs["event_type"] == "test.event"
    assert publisher._sender is execution_dependencies.publisher
    assert kwargs["session"] is not None


def test_shares_configured_database_with_relay_and_failure_handler(
    execution_dependencies,
):
    """DB設定から作ったsession factoryをrelayと失敗処理へ共有する。"""
    asyncio.run(
        execution.run_relay(
            execution_dependencies.settings, execution_dependencies.route
        )
    )
    execution_dependencies.create_engine.assert_called_once_with(
        execution_dependencies.settings
    )
    execution_dependencies.create_factory.assert_called_once_with(
        execution_dependencies.engine
    )
    execution_dependencies.create_failure_handler.assert_called_once()
    execution_dependencies.create_relay.assert_called_once()
    # 引数の位置・キーワードの選択に依存せず、接続した部品を確認する。
    for call in (
        execution_dependencies.create_failure_handler.call_args,
        execution_dependencies.create_relay.call_args,
    ):
        actual = (*call.args, *call.kwargs.values())
        assert any(value is execution_dependencies.factory for value in actual)


def test_runs_once_and_completes_only_after_disposal(execution_dependencies):
    """relayを1回実行し、Engineの終了後に完了応答を返す。"""
    assert asyncio.run(
        execution.run_relay(
            execution_dependencies.settings, execution_dependencies.route
        )
    ) == {"status": "completed"}
    execution_dependencies.relay.run_once.assert_awaited_once_with()
    execution_dependencies.engine.dispose.assert_awaited_once_with()
    assert execution_dependencies.trace == ["run_once", "disposed"]


@pytest.mark.parametrize("phase", ["assembly", "run"])
def test_failure_propagates_after_engine_disposal(execution_dependencies, phase):
    """組み立て・実行の失敗を伝播し、生成済みEngineを終了する。"""
    original = RuntimeError("test failure")
    operation = (
        execution_dependencies.create_failure_handler
        if phase == "assembly"
        else execution_dependencies.relay.run_once
    )
    operation.side_effect = original
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(
            execution.run_relay(
                execution_dependencies.settings, execution_dependencies.route
            )
        )
    assert caught.value is original
    execution_dependencies.engine.dispose.assert_awaited_once_with()


def test_disposal_failure_prevents_success_response(execution_dependencies):
    """実行後にEngineを終了できなかった場合は完了を返さない。"""
    error = RuntimeError("dispose failure")
    execution_dependencies.engine.dispose.side_effect = error
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(
            execution.run_relay(
                execution_dependencies.settings, execution_dependencies.route
            )
        )
    assert caught.value is error
    execution_dependencies.relay.run_once.assert_awaited_once_with()


@pytest.mark.parametrize(
    "original", [RuntimeError("run failure"), asyncio.CancelledError()]
)
def test_disposal_failure_does_not_replace_run_failure(
    execution_dependencies, original
):
    """終了が失敗しても、先行する実行例外・外部キャンセルを維持する。"""
    execution_dependencies.relay.run_once.side_effect = original
    execution_dependencies.engine.dispose.side_effect = ValueError("dispose failure")
    with pytest.raises(type(original)) as caught:
        asyncio.run(
            execution.run_relay(
                execution_dependencies.settings, execution_dependencies.route
            )
        )
    assert caught.value is original
    execution_dependencies.engine.dispose.assert_awaited_once_with()
