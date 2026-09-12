"""記事単位AI分析の資源所有と初期化・終了の境界を確認する。"""

import asyncio
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr

from app.aws import ssm
from app.db import iam
from app.lambda_handlers import article_analysis_lifecycle as module
from app.lambda_handlers.assessment.failure_recorder import (
    AssessmentLambdaFailureRecorder,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def lifecycle(monkeypatch):
    order = []
    log = Mock()
    state = SimpleNamespace(order=order, log=log)

    def secret(**kwargs):
        order.append("secret")
        return SecretStr("private-key")

    state.secret = Mock(side_effect=secret)
    monkeypatch.setattr(module, "get_secret_parameter", state.secret)
    state.rds_clients = []

    def create_rds(*args, **kwargs):
        order.append("rds")
        rds = Mock()
        rds.generate_db_auth_token.return_value = "signed-token"
        rds.close.side_effect = lambda: order.append("rds_close")
        state.rds_clients.append(rds)
        return rds

    state.session = Mock()
    state.session.create_client.side_effect = create_rds
    monkeypatch.setattr(module, "Session", Mock(return_value=state.session))
    state.signer = Mock(wraps=module.build_iam_password_provider)
    monkeypatch.setattr(module, "build_iam_password_provider", state.signer)
    monkeypatch.setattr(
        iam, "_rds_client", Mock(side_effect=AssertionError("global signer"))
    )
    state.engines = []

    def create_engine(**kwargs):
        order.append("engine")
        engine = SimpleNamespace(
            dispose=AsyncMock(side_effect=lambda: order.append("engine_close"))
        )
        state.engines.append(engine)
        return engine

    state.create_engine = Mock(side_effect=create_engine)

    def session_factory(engine):
        order.append("session_factory")
        return Mock()

    state.session_factory = Mock(side_effect=session_factory)
    monkeypatch.setattr(module, "caller_managed_session_factory", state.session_factory)
    state.clients = []
    state.client_close = AsyncMock(side_effect=lambda: order.append("client_close"))

    @asynccontextmanager
    async def open_client(**kwargs):
        order.append("client")
        client = object()
        state.clients.append(client)
        try:
            yield client
        finally:
            await state.client_close()

    state.open_client = Mock(side_effect=open_client)

    def build_consumer(**kwargs):
        order.append("consumer")
        return SimpleNamespace(**kwargs)

    state.build_consumer = Mock(side_effect=build_consumer)
    state.arguments = dict(
        aws_region="ap-northeast-1",
        database_url="postgresql+asyncpg://vector_app@db.invalid:5432/vector?sslmode=require",
        api_key_parameter_path="/test/key",
        create_engine=state.create_engine,
        open_client=state.open_client,
        build_consumer=state.build_consumer,
        failure_recorder=AssessmentLambdaFailureRecorder(log),
    )

    def open_consumer():
        return module.open_article_analysis_consumer(**state.arguments)

    state.open = open_consumer
    return state


@pytest.mark.asyncio
async def test_borrows_prepared_client_and_session_factory(lifecycle):
    """Consumerには共通側で準備した依存をそのまま貸し出す。"""
    async with lifecycle.open() as consumer:
        assert consumer.client is lifecycle.clients[0]
        assert (
            consumer.session_factory
            is lifecycle.build_consumer.call_args.kwargs["session_factory"]
        )
        lifecycle.session_factory.assert_called_once_with(lifecycle.engines[0])
        lifecycle.open_client.assert_called_once_with(api_key=SecretStr("private-key"))
    lifecycle.secret.assert_called_once_with(region="ap-northeast-1", path="/test/key")


@pytest.mark.asyncio
async def test_uses_invocation_signer_with_unchanged_sdk_configuration(lifecycle):
    """共通の署名器キャッシュを使わず、呼び出し専用RDSへ毎回署名を依頼する。"""
    async with lifecycle.open():
        provide = lifecycle.create_engine.call_args.kwargs["password_provider"]
        assert await provide() == await provide() == "signed-token"
        rds = lifecycle.rds_clients[0]
        assert rds.generate_db_auth_token.call_count == 2
        rds.generate_db_auth_token.assert_called_with(
            DBHostname="db.invalid", Port=5432, DBUsername="vector_app"
        )
        args = lifecycle.session.create_client.call_args
        assert args.args == ("rds",)
        assert args.kwargs["region_name"] == "ap-northeast-1"
        assert args.kwargs["config"].proxies == {}
        assert args.kwargs["config"].ignore_configured_endpoint_urls is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase,stage,closed",
    [
        ("secret", "resources", []),
        ("rds", "resources", []),
        ("signer", "resources", ["rds_close"]),
        ("create_engine", "resources", ["rds_close"]),
        ("session_factory", "resources", ["engine_close", "rds_close"]),
        ("open_client", "ai_client", ["engine_close", "rds_close"]),
    ],
)
async def test_initialization_failure_closes_only_acquired_resources(
    lifecycle, phase, stage, closed
):
    """各初期化段階の失敗は取得済み資源を解放して同じ例外を返す。"""
    original = RuntimeError("private-initialization")
    failing = (
        lifecycle.session.create_client if phase == "rds" else getattr(lifecycle, phase)
    )
    failing.side_effect = original
    with pytest.raises(RuntimeError) as caught:
        async with lifecycle.open():
            pytest.fail("初期化失敗時に貸し出してはいけない")
    assert caught.value is original
    assert [event for event in lifecycle.order if event.endswith("_close")] == closed
    lifecycle.log.warning.assert_called_once_with(
        "assessment_initialization_failed",
        stage=stage,
        error_class="builtins.RuntimeError",
    )


@pytest.mark.asyncio
async def test_initialization_log_failure_preserves_original(lifecycle):
    """診断出力が失敗しても初期化の元例外を保持する。"""
    original = RuntimeError("original")
    lifecycle.build_consumer.side_effect = original
    lifecycle.log.warning.side_effect = RuntimeError("log-failed")
    with pytest.raises(RuntimeError) as caught:
        async with lifecycle.open():
            pytest.fail("初期化失敗時に貸し出してはいけない")
    assert caught.value is original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        KeyboardInterrupt(),
        SystemExit(),
    ],
)
async def test_borrower_failure_is_not_initialization_failure(lifecycle, failure):
    """利用中の例外・キャンセル・終了要求は初期化失敗に変換しない。"""
    with pytest.raises(type(failure)) as caught:
        async with lifecycle.open():
            raise failure
    assert caught.value is failure
    lifecycle.log.warning.assert_not_called()
    assert lifecycle.order[-3:] == ["client_close", "engine_close", "rds_close"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [None, RuntimeError("original"), asyncio.CancelledError()]
)
@pytest.mark.parametrize("log_fails", [False, True])
async def test_cleanup_failures_preserve_borrower_outcome(
    lifecycle, failure, log_fails
):
    """通常の終了・ログ障害は後続解放を妨げず、利用結果を保持する。"""
    if log_fails:
        lifecycle.log.warning.side_effect = RuntimeError("private-log")

    async def run():
        async with lifecycle.open():
            lifecycle.engines[0].dispose.side_effect = RuntimeError("private-engine")
            lifecycle.rds_clients[0].close.side_effect = RuntimeError("private-rds")
            if failure is not None:
                raise failure
        return "done"

    if failure is None:
        assert await run() == "done"
    else:
        with pytest.raises(type(failure)) as caught:
            await run()
        assert caught.value is failure
    lifecycle.engines[0].dispose.assert_awaited_once()
    lifecycle.rds_clients[0].close.assert_called_once()
    assert [
        call.kwargs["resource"] for call in lifecycle.log.warning.call_args_list
    ] == ["engine", "rds"]
    assert "private" not in repr(lifecycle.log.warning.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit()]
)
async def test_initialization_interrupt_propagates(lifecycle, failure):
    """初期化中の中断を通常失敗として記録せず、取得済み資源を解放する。"""
    lifecycle.open_client.side_effect = failure
    with pytest.raises(type(failure)) as caught:
        async with lifecycle.open():
            pytest.fail("中断時に貸し出してはいけない")
    assert caught.value is failure
    lifecycle.log.warning.assert_not_called()
    assert lifecycle.order[-2:] == ["engine_close", "rds_close"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resource,closed",
    [
        ("client", ["engine_close", "rds_close"]),
        ("engine", ["client_close", "rds_close"]),
        ("rds", ["client_close", "engine_close"]),
    ],
)
async def test_cleanup_cancellation_propagates_and_releases_remaining(
    lifecycle, resource, closed
):
    """終了中のキャンセルを伝播し、残った外側資源の解放を試みる。"""
    original = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError) as caught:
        async with lifecycle.open():
            close = {
                "client": lifecycle.client_close,
                "engine": lifecycle.engines[0].dispose,
                "rds": lifecycle.rds_clients[0].close,
            }[resource]
            close.side_effect = original
    assert caught.value is original
    actual = [event for event in lifecycle.order if event.endswith("_close")]
    assert actual == closed
    lifecycle.log.warning.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_ssm_thread_closes_its_client(lifecycle, monkeypatch):
    """SSM待機のキャンセル後も取得スレッド自身が通信資源を閉じる。"""
    started, release, closed = threading.Event(), threading.Event(), threading.Event()
    sdk = Mock()

    def get_parameter(**kwargs):
        started.set()
        if not release.wait(2):
            raise RuntimeError("test release timeout")
        return {"Parameter": {"Value": "private"}}

    sdk.get_parameter.side_effect = get_parameter
    sdk.close.side_effect = closed.set
    session = Mock()
    session.create_client.return_value = sdk
    monkeypatch.setattr(ssm, "Session", Mock(return_value=session))
    monkeypatch.setattr(module, "get_secret_parameter", ssm.get_secret_parameter)

    async def run():
        async with lifecycle.open():
            pytest.fail("中断時に貸し出してはいけない")

    task = asyncio.create_task(run())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    assert await asyncio.to_thread(closed.wait, 2)
    sdk.close.assert_called_once()
    lifecycle.session.create_client.assert_not_called()
