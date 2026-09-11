"""呼び出し単位の秘密情報・DB資源の組み立てを検証する。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr, ValidationError

from app.db import engine as engine_module
from app.db import iam
from app.lambda_handlers.assessment import resources as module
from app.lambda_handlers.assessment.settings import AssessmentConsumerSettings


def settings(**overrides):
    return AssessmentConsumerSettings(
        **{
            "env": "production",
            "database_url": "postgresql+asyncpg://vector_app@db.invalid:5432/vector?sslmode=require",
            "db_iam_auth": True,
            "aws_region": "ap-northeast-1",
            "deepseek_api_key_parameter_path": (
                "/vector/assessment-consumer/deepseek-api-key"
            ),
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"db_iam_auth": False},
        {"env": "test", "db_iam_auth": False},
        {"env": "development", "db_iam_auth": False},
        {"database_url": "postgresql+asyncpg://vector_app@db.invalid/vector"},
        {
            "database_url": "postgresql+asyncpg://vector_app:private@db.invalid/vector?sslmode=require"
        },
        {"aws_region": " "},
        {"deepseek_api_key_parameter_path": " "},
    ],
)
def test_invalid_production_settings(overrides):
    """全環境でIAM無効を拒否し、本番TLS・URL内パスワード・空白設定も検証する。"""
    with pytest.raises(ValidationError):
        settings(**overrides)


def test_settings_hide_url_and_do_not_load_dotenv():
    """設定の表示に接続先を含めず、dotenv読込を無効にし、テスト環境でもIAMを必須にする。"""
    config = settings()
    assert config.model_config["env_file"] is None
    assert "db.invalid" not in repr(config)
    with pytest.raises(ValidationError):
        settings(env="test", db_iam_auth=False)


@pytest.fixture
def mocks(monkeypatch):
    rds = Mock()
    rds.generate_db_auth_token.return_value = "signed-token"
    session = Mock()
    session.create_client.return_value = rds
    monkeypatch.setattr(module, "Session", Mock(return_value=session))
    secret = Mock(return_value=SecretStr("private-key"))
    monkeypatch.setattr(module, "get_secret_parameter", secret)
    engine = SimpleNamespace(dispose=AsyncMock())
    create = Mock(return_value=engine)
    monkeypatch.setattr(module, "create_assessment_consumer_engine", create)
    factory = Mock(return_value=Mock())
    monkeypatch.setattr(module, "caller_managed_session_factory", factory)
    monkeypatch.setattr(
        iam,
        "_rds_client",
        Mock(side_effect=AssertionError("global cache must not be used")),
    )
    signer = Mock(wraps=module.build_iam_password_provider)
    monkeypatch.setattr(module, "build_iam_password_provider", signer)
    return SimpleNamespace(
        signer=signer,
        rds=rds,
        session=session,
        secret=secret,
        engine=engine,
        create=create,
        factory=factory,
    )


@pytest.mark.asyncio
async def test_invocation_resources_and_injected_signer(mocks):
    """秘密情報とセッション生成器を返し、専用RDS署名器で接続トークンを生成する。"""
    config = settings()
    async with module.open_assessment_resources(config) as resources:
        assert resources.deepseek_api_key.get_secret_value() == "private-key"
        assert "private-key" not in repr(resources)
        assert resources.session_factory is mocks.factory.return_value
        provider = mocks.create.call_args.kwargs["password_provider"]
        assert await provider() == "signed-token"
        assert await provider() == "signed-token"
        assert mocks.rds.generate_db_auth_token.call_count == 2
        mocks.rds.generate_db_auth_token.assert_called_with(
            DBHostname="db.invalid", Port=5432, DBUsername="vector_app"
        )
        assert (
            mocks.session.create_client.call_args.kwargs["region_name"]
            == "ap-northeast-1"
        )
        mocks.rds.close.assert_not_called()
    mocks.engine.dispose.assert_awaited_once()
    mocks.rds.close.assert_called_once()
    mocks.secret.assert_called_once_with(
        region=config.aws_region, path=config.deepseek_api_key_parameter_path
    )


@pytest.mark.asyncio
async def test_separate_scopes_refetch_and_recreate(mocks):
    """利用範囲ごとに秘密情報を再取得してDB資源を作り直し、各資源を終了する。"""
    mocks.secret.side_effect = [SecretStr("first"), SecretStr("second")]
    keys = []
    for _ in range(2):
        async with module.open_assessment_resources(settings()) as resources:
            keys.append(resources.deepseek_api_key.get_secret_value())
    assert keys == ["first", "second"]
    assert mocks.secret.call_count == mocks.create.call_count == 2
    assert mocks.rds.close.call_count == mocks.engine.dispose.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["secret", "rds", "signer", "engine", "factory"])
async def test_partial_initialization_failure(mocks, phase):
    """初期化の各段階で失敗したとき、作成済みの資源だけを閉じて元の例外を返す。"""
    failure = RuntimeError("original")
    {
        "secret": mocks.secret,
        "rds": mocks.session.create_client,
        "signer": mocks.signer,
        "engine": mocks.create,
        "factory": mocks.factory,
    }[phase].side_effect = failure
    with pytest.raises(RuntimeError) as caught:
        async with module.open_assessment_resources(settings()):
            pytest.fail("must not yield")
    assert caught.value is failure
    assert mocks.rds.close.call_count == (
        1 if phase in ("signer", "engine", "factory") else 0
    )
    assert mocks.engine.dispose.await_count == (1 if phase == "factory" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [None, RuntimeError("original"), asyncio.CancelledError()]
)
@pytest.mark.parametrize("log_fails", [False, True])
async def test_cleanup_preserves_result_and_exception(
    mocks, monkeypatch, failure, log_fails
):
    """終了と診断が失敗しても残りを閉じ、利用の成功・元例外・キャンセルを保持する。"""
    mocks.engine.dispose.side_effect = RuntimeError("private")
    mocks.rds.close.side_effect = RuntimeError("private")
    log = Mock(side_effect=RuntimeError("log") if log_fails else None)
    monkeypatch.setattr(module.logger, "warning", log)

    async def run():
        async with module.open_assessment_resources(settings()):
            if failure is not None:
                raise failure
        return "completed"

    if failure is None:
        assert await run() == "completed"
    else:
        with pytest.raises(type(failure)) as caught:
            await run()
        assert caught.value is failure
    assert "private" not in repr(log.call_args_list)
    assert log.call_count == 2
    mocks.rds.close.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_cancellation_propagates(mocks):
    """Engine終了中のキャンセルを伝播し、残ったRDSクライアントも閉じる。"""
    mocks.engine.dispose.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        async with module.open_assessment_resources(settings()):
            pass
    mocks.rds.close.assert_called_once()


def test_engine_configuration_preserves_iam_tls(monkeypatch):
    """接続数1・待機上限5秒・IAM署名器・TLS・Assessment識別名をEngineへ設定する。"""
    create = Mock()
    monkeypatch.setattr(engine_module, "create_async_engine", create)
    provider = AsyncMock(return_value="token")
    engine_module.create_assessment_consumer_engine(
        settings(), password_provider=provider
    )
    kwargs = create.call_args.kwargs
    assert (kwargs["pool_size"], kwargs["max_overflow"], kwargs["pool_timeout"]) == (
        1,
        0,
        5,
    )
    assert kwargs["pool_pre_ping"] and kwargs["hide_parameters"]
    assert (
        kwargs["connect_args"]["timeout"]
        == kwargs["connect_args"]["command_timeout"]
        == 5
    )
    assert kwargs["connect_args"]["password"] is provider
    assert kwargs["connect_args"]["ssl"]
    assert (
        kwargs["connect_args"]["server_settings"]["application_name"]
        == "vector-assessment-consumer"
    )
    assert "sslmode" not in create.call_args.args[0]
    with pytest.raises(TypeError):
        engine_module.create_assessment_consumer_engine(settings())
    with pytest.raises(TypeError):
        engine_module.create_assessment_consumer_engine(
            settings(), password_provider=None
        )


@pytest.mark.parametrize(
    "field", ["aws_region", "database_url", "deepseek_api_key_parameter_path"]
)
def test_required_settings_are_not_optional(monkeypatch, field):
    """必須設定が環境にも引数にも存在しない場合、準備を開始せず検証エラーにする。"""
    monkeypatch.delenv(field.upper(), raising=False)
    values = settings().model_dump()
    del values[field]
    with pytest.raises(ValidationError):
        AssessmentConsumerSettings(**values)


def test_validation_message_does_not_expose_database_url():
    """設定検証の例外文字列には接続先やURL内のパスワードを表示しない。"""
    with pytest.raises(ValidationError) as caught:
        settings(
            database_url="postgresql+asyncpg://user:private-password@private-host/db?sslmode=require"
        )
    assert "private-password" not in str(caught.value)
    assert "private-host" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("body_fails", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_composed_resources_close_in_order_and_keep_result(
    mocks, monkeypatch, body_fails, cleanup_fails
):
    """実際の組み立て経路でDeepSeek・Engine・RDSを順に閉じ、終了障害で利用結果を変えない。"""
    from app.ai_providers.deepseek import client as client_module
    from app.lambda_handlers.assessment import composition

    order = []
    mocks.secret.side_effect = lambda **kwargs: (
        order.append("secret"),
        SecretStr("private"),
    )[1]
    mocks.session.create_client.side_effect = lambda *args, **kwargs: (
        order.append("rds"),
        mocks.rds,
    )[1]
    mocks.create.side_effect = lambda *args, **kwargs: (
        order.append("engine"),
        mocks.engine,
    )[1]
    sdk = Mock()
    sdk.close = AsyncMock()
    http = Mock()
    http.is_closed = False

    def close_http():
        order.append("deepseek")
        http.is_closed = True
        if cleanup_fails:
            raise RuntimeError("private-close")

    http.aclose = AsyncMock(side_effect=close_http)
    sdk.close.side_effect = http.aclose

    def close_engine():
        order.append("engine")
        if cleanup_fails:
            raise RuntimeError("private-engine")

    def close_rds():
        order.append("rds")
        if cleanup_fails:
            raise RuntimeError("private-rds")

    mocks.engine.dispose.side_effect = close_engine
    mocks.rds.close.side_effect = close_rds
    monkeypatch.setattr(
        client_module, "make_external_async_client", Mock(return_value=http)
    )
    monkeypatch.setattr(
        client_module,
        "AsyncOpenAI",
        Mock(side_effect=lambda **kwargs: (order.append("deepseek"), sdk)[1]),
    )
    for logger in (module.logger, client_module.logger):
        monkeypatch.setattr(
            logger, "warning", Mock(side_effect=RuntimeError("private-log"))
        )
    initialization_log = Mock()
    monkeypatch.setattr(composition.logger, "warning", initialization_log)
    original = ValueError("business")

    async def run():
        async with composition.open_assessment_consumer(settings()):
            if body_fails:
                raise original
        return "done"

    if body_fails:
        with pytest.raises(ValueError) as caught:
            await run()
        assert caught.value is original
    else:
        assert await run() == "done"
    assert order == ["secret", "rds", "engine", "deepseek", "deepseek", "engine", "rds"]
    initialization_log.assert_not_called()


def test_settings_read_only_assessment_inputs(monkeypatch):
    """宣言した環境設定だけを読み、他工程のAPIキーやアプリ設定を取り込まない。"""
    expected = {
        "ENV": "production",
        "AWS_REGION": "ap-northeast-1",
        "DATABASE_URL": "postgresql+asyncpg://vector_app@db.invalid/vector?sslmode=require",
        "DB_IAM_AUTH": "true",
        "DEEPSEEK_API_KEY_PARAMETER_PATH": "/assessment/key",
    }
    for name, value in expected.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GEMINI_API_KEY_PARAMETER_PATH", "/other/key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unrelated-private")
    config = AssessmentConsumerSettings()
    assert config.model_dump() == {
        "env": "production",
        "aws_region": "ap-northeast-1",
        "database_url": expected["DATABASE_URL"],
        "db_iam_auth": True,
        "deepseek_api_key_parameter_path": "/assessment/key",
    }
    assert "unrelated-private" not in repr(config)
