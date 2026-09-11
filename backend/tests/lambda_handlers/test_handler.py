"""Lambda入口の組み立て・実行・終了の境界を確認する。"""

from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.lambda_handlers.outbox_relay.settings import OutboxRelaySettings

entry = import_module("app.lambda_handlers.outbox_relay.handler")

pytestmark = pytest.mark.unit


@pytest.fixture
def handler_dependencies(monkeypatch):
    settings = OutboxRelaySettings(
        env="test",
        database_url="postgresql+asyncpg://user@database.invalid/vector",
        db_iam_auth=False,
        aws_region="ap-northeast-1",
        **{
            f"sqs_article_{stage}_queue_url": f"https://sqs.invalid/{stage}"
            for stage in ("completion", "curation", "assessment", "embedding")
        },
    )
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
    monkeypatch.setattr(entry, "OutboxRelaySettings", Mock(return_value=settings))
    monkeypatch.setattr(entry, "create_lambda_engine", create_engine)
    monkeypatch.setattr(
        entry, "caller_managed_session_factory", create_factory, raising=False
    )
    monkeypatch.setattr(
        entry, "SqsSender", Mock(from_session=create_publisher), raising=False
    )
    monkeypatch.setattr(
        entry, "OutboxDeliveryFailureHandler", create_failure_handler, raising=False
    )
    monkeypatch.setattr(entry, "OutboxRelay", create_relay, raising=False)
    return SimpleNamespace(**locals())


def test_passes_delivery_settings_to_publisher(handler_dependencies):
    """設定のregionとQueue URLをpublisherの生成へ渡す。"""
    entry.handler({}, None)
    handler_dependencies.create_publisher.assert_called_once()
    kwargs = handler_dependencies.create_publisher.call_args.kwargs
    assert kwargs["region"] == handler_dependencies.settings.aws_region
    relay_call = handler_dependencies.create_relay.call_args
    publisher = relay_call.args[1]
    assert (
        publisher._route.queue_url
        == handler_dependencies.settings.sqs_article_embedding_queue_url
    )
    assert (
        relay_call.kwargs["event_type"]
        == publisher._route.event_type
        == "article.assessed_in_scope"
    )
    assert publisher._route.build_message is entry.build_assessed_in_scope_message
    assert publisher._sender is handler_dependencies.publisher
    assert kwargs["session"] is not None


def test_shares_configured_database_with_relay_and_failure_handler(
    handler_dependencies,
):
    """DB設定から作ったsession factoryをrelayと失敗処理へ共有する。"""
    entry.handler({}, None)
    handler_dependencies.create_engine.assert_called_once_with(
        handler_dependencies.settings
    )
    handler_dependencies.create_factory.assert_called_once_with(
        handler_dependencies.engine
    )
    handler_dependencies.create_failure_handler.assert_called_once()
    handler_dependencies.create_relay.assert_called_once()
    # 引数の位置・キーワードの選択に依存せず、接続した部品を確認する。
    for call in (
        handler_dependencies.create_failure_handler.call_args,
        handler_dependencies.create_relay.call_args,
    ):
        actual = (*call.args, *call.kwargs.values())
        assert any(value is handler_dependencies.factory for value in actual)


def test_runs_once_and_completes_only_after_disposal(handler_dependencies):
    """relayを1回実行し、Engineの終了後に完了応答を返す。"""
    assert entry.handler({}, None) == {"status": "completed"}
    handler_dependencies.relay.run_once.assert_awaited_once_with()
    handler_dependencies.engine.dispose.assert_awaited_once_with()
    assert handler_dependencies.trace == ["run_once", "disposed"]


def test_invalid_settings_prevent_engine_creation(handler_dependencies):
    """設定構築に失敗した起動はDBリソースを作らない。"""
    error = ValidationError.from_exception_data("OutboxRelaySettings", [])
    entry.OutboxRelaySettings.side_effect = error
    with pytest.raises(ValidationError) as caught:
        entry.handler({}, None)
    assert caught.value is error
    handler_dependencies.create_engine.assert_not_called()


@pytest.mark.parametrize("phase", ["assembly", "run"])
def test_failure_propagates_after_engine_disposal(handler_dependencies, phase):
    """組み立て・実行の失敗を伝播し、生成済みEngineを終了する。"""
    original = RuntimeError("test failure")
    operation = (
        handler_dependencies.create_failure_handler
        if phase == "assembly"
        else handler_dependencies.relay.run_once
    )
    operation.side_effect = original
    with pytest.raises(RuntimeError) as caught:
        entry.handler({}, None)
    assert caught.value is original
    handler_dependencies.engine.dispose.assert_awaited_once_with()


def test_disposal_failure_prevents_success_response(handler_dependencies):
    """実行後にEngineを終了できなかった場合は完了を返さない。"""
    error = RuntimeError("dispose failure")
    handler_dependencies.engine.dispose.side_effect = error
    with pytest.raises(RuntimeError) as caught:
        entry.handler({}, None)
    assert caught.value is error
    handler_dependencies.relay.run_once.assert_awaited_once_with()


def test_disposal_failure_does_not_replace_run_failure(handler_dependencies):
    """実行と終了がともに失敗しても、先行する実行の例外を伝える。"""
    original = RuntimeError("run failure")
    handler_dependencies.relay.run_once.side_effect = original
    handler_dependencies.engine.dispose.side_effect = ValueError("dispose failure")
    with pytest.raises(RuntimeError) as caught:
        entry.handler({}, None)
    assert caught.value is original
    handler_dependencies.engine.dispose.assert_awaited_once_with()
