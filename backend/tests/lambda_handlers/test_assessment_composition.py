"""Consumerの配線と、初期化・利用・終了の例外境界を検証する。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from app.ai_providers.deepseek.settings import DeepSeekConnectionSettings
from app.analysis.assessment.ai.spec import DEEPSEEK_ASSESSMENT_SPEC
from app.lambda_handlers.assessment import composition as module


@pytest.fixture
def wiring(monkeypatch):
    order = []
    resources = SimpleNamespace(
        deepseek_api_key=SecretStr("private"), session_factory=Mock()
    )
    client = Mock()
    state = SimpleNamespace(
        order=order, resources=resources, client=client, failure=None, phase=None
    )

    @asynccontextmanager
    async def open_resources(settings):
        order.append("resources")
        if state.phase == "resources":
            raise state.failure
        try:
            yield resources
        finally:
            order.extend(["engine_close", "rds_close"])

    @asynccontextmanager
    async def open_client(**kwargs):
        order.append("deepseek_client")
        assert kwargs == {
            "api_key": resources.deepseek_api_key,
            "base_url": DEEPSEEK_ASSESSMENT_SPEC.base_url,
            "settings": DeepSeekConnectionSettings(),
        }
        if state.phase == "deepseek_client":
            raise state.failure
        try:
            yield client
        finally:
            order.append("deepseek_close")

    monkeypatch.setattr(module, "open_assessment_resources", open_resources)
    monkeypatch.setattr(module, "open_deepseek_client", open_client)
    state.log = Mock()
    monkeypatch.setattr(module.logger, "warning", state.log)
    return state


@pytest.mark.asyncio
async def test_wires_borrowed_client_and_session_factory(wiring):
    """実際のAssessorとConsumerへ同じ借用クライアントとsession factoryを渡す。"""
    async with module.open_assessment_consumer(Mock()) as consumer:
        assert consumer._session_factory is wiring.resources.session_factory
        assert consumer._assessor._client is wiring.client
        assert wiring.order == ["resources", "deepseek_client"]
    assert wiring.order[-3:] == ["deepseek_close", "engine_close", "rds_close"]
    wiring.log.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase", ["resources", "deepseek_client", "assessor", "consumer"]
)
@pytest.mark.parametrize("log_fails", [False, True])
async def test_initialization_preserves_exception_and_closes_created_resources(
    wiring, monkeypatch, phase, log_fails
):
    """各初期化段階の失敗を安全に記録し、作成済み資源を閉じて同じ例外を返す。"""
    failure = RuntimeError("private-secret")
    wiring.phase = phase
    wiring.failure = failure
    if phase in ("assessor", "consumer"):
        name = "DeepSeekAssessor" if phase == "assessor" else "AssessmentConsumer"
        monkeypatch.setattr(module, name, Mock(side_effect=failure))
    if log_fails:
        wiring.log.side_effect = RuntimeError("private-log")
    with pytest.raises(RuntimeError) as caught:
        async with module.open_assessment_consumer(Mock()):
            pytest.fail("must not yield")
    assert caught.value is failure
    stage = "consumer" if phase == "assessor" else phase
    wiring.log.assert_called_once_with(
        "assessment_initialization_failed",
        stage=stage,
        error_class="builtins.RuntimeError",
    )
    assert "private" not in repr(wiring.log.call_args_list)
    if phase == "resources":
        assert wiring.order == ["resources"]
    elif phase == "deepseek_client":
        assert wiring.order[-2:] == ["engine_close", "rds_close"]
    else:
        assert wiring.order[-3:] == ["deepseek_close", "engine_close", "rds_close"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [ValueError("business"), asyncio.CancelledError(), SystemExit()]
)
async def test_body_failure_is_not_initialization_failure(wiring, failure):
    """利用側の例外・キャンセル・終了要求は初期化失敗とせず、資源を閉じて伝播する。"""
    with pytest.raises(type(failure)) as caught:
        async with module.open_assessment_consumer(Mock()):
            raise failure
    assert caught.value is failure
    wiring.log.assert_not_called()
    assert wiring.order[-3:] == ["deepseek_close", "engine_close", "rds_close"]


@pytest.mark.asyncio
async def test_initialization_cancellation_is_not_recorded_as_failure(wiring):
    """初期化中のキャンセルを通常エラーとして記録せず、取得済み資源を閉じる。"""
    wiring.phase = "deepseek_client"
    wiring.failure = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        async with module.open_assessment_consumer(Mock()):
            pytest.fail("must not yield")
    wiring.log.assert_not_called()
    assert wiring.order[-2:] == ["engine_close", "rds_close"]
