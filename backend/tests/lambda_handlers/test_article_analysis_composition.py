"""工程別の設定・SDK・Consumerが共通ライフサイクルへ正しく接続されることを確認する。"""

from contextlib import asynccontextmanager
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr

from app.lambda_handlers import article_analysis_lifecycle as lifecycle

pytestmark = pytest.mark.unit


@pytest.fixture(params=["assessment", "embedding"])
def wiring(request, monkeypatch):
    stage = request.param
    provider = "deepseek" if stage == "assessment" else "gemini"
    provider_title = "DeepSeek" if stage == "assessment" else "Gemini"
    module = import_module(f"app.lambda_handlers.{stage}.composition")
    settings_type = getattr(module, f"{stage.title()}ConsumerSettings")
    settings = settings_type(
        env="test",
        aws_region="ap-northeast-1",
        database_url="postgresql+asyncpg://vector_app@db.invalid/vector",
        db_iam_auth=True,
        **{f"{provider}_api_key_parameter_path": f"/{stage}/key"},
    )
    rds = Mock()
    session = Mock()
    session.create_client.return_value = rds
    monkeypatch.setattr(lifecycle, "Session", Mock(return_value=session))
    secret = Mock(return_value=SecretStr("test-key"))
    monkeypatch.setattr(lifecycle, "get_secret_parameter", secret)
    engine = SimpleNamespace(dispose=AsyncMock())
    create_engine = Mock(return_value=engine)
    monkeypatch.setattr(module, f"create_{stage}_consumer_engine", create_engine)
    factory = Mock()
    monkeypatch.setattr(
        lifecycle, "caller_managed_session_factory", Mock(return_value=factory)
    )
    sdk_client = Mock()

    @asynccontextmanager
    async def open_client(**kwargs):
        yield sdk_client

    client_factory = Mock(side_effect=open_client)
    monkeypatch.setattr(module, f"open_{provider}_client", client_factory)
    log = Mock()
    monkeypatch.setattr(module, "logger", log)
    return SimpleNamespace(
        module=module,
        stage=stage,
        provider=provider,
        provider_title=provider_title,
        settings=settings,
        secret=secret,
        factory=factory,
        sdk_client=sdk_client,
        create_engine=create_engine,
        open_client=client_factory,
        log=log,
        open=getattr(module, f"open_{stage}_consumer"),
    )


@pytest.mark.asyncio
async def test_passes_stage_configuration_to_delayed_factories(wiring):
    """工程設定を捕捉する関数は共通context managerに入るまで資源を作らない。"""
    context = wiring.open(wiring.settings)
    wiring.secret.assert_not_called()
    wiring.create_engine.assert_not_called()
    wiring.open_client.assert_not_called()
    async with context:
        wiring.secret.assert_called_once_with(
            region=wiring.settings.aws_region, path=f"/{wiring.stage}/key"
        )
        assert wiring.create_engine.call_args.args == (wiring.settings,)
        assert callable(wiring.create_engine.call_args.kwargs["password_provider"])
        expected = dict(
            api_key=SecretStr("test-key"),
            settings=getattr(
                wiring.module, f"{wiring.provider_title}ConnectionSettings"
            )(),
        )
        if wiring.stage == "assessment":
            expected["base_url"] = wiring.module.DEEPSEEK_ASSESSMENT_SPEC.base_url
        wiring.open_client.assert_called_once_with(**expected)


@pytest.mark.asyncio
async def test_builds_real_consumer_with_borrowed_dependencies(wiring):
    """各工程の実Consumer・AI実装に準備済みクライアントとsession factoryを渡す。"""
    async with wiring.open(wiring.settings) as consumer:
        assert isinstance(
            consumer, getattr(wiring.module, f"{wiring.stage.title()}Consumer")
        )
        assert consumer._session_factory is wiring.factory
        ai = consumer._assessor if wiring.stage == "assessment" else consumer._embedder
        assert ai._client is wiring.sdk_client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dependency,expected_stage",
    [
        ("secret", "resources"),
        ("open_client", "ai_client"),
        ("ai", "consumer"),
        ("consumer", "consumer"),
        ("client_settings", "ai_client"),
    ],
)
async def test_initialization_diagnostics_preserve_stage_identity(
    wiring, monkeypatch, dependency, expected_stage
):
    """工程固有の診断名を維持し、AI準備の診断段階を共通名で記録する。"""
    original = RuntimeError("private-initialization")
    if dependency in ("secret", "open_client"):
        getattr(wiring, dependency).side_effect = original
    else:
        name = {
            "ai": "DeepSeekAssessor"
            if wiring.stage == "assessment"
            else "GeminiEmbedder",
            "consumer": f"{wiring.stage.title()}Consumer",
            "client_settings": f"{wiring.provider_title}ConnectionSettings",
        }[dependency]
        monkeypatch.setattr(wiring.module, name, Mock(side_effect=original))
    with pytest.raises(RuntimeError) as caught:
        async with wiring.open(wiring.settings):
            pytest.fail("初期化失敗時に貸し出してはいけない")
    assert caught.value is original
    wiring.log.warning.assert_called_once_with(
        f"{wiring.stage}_initialization_failed",
        stage=expected_stage,
        error_class="builtins.RuntimeError",
    )
