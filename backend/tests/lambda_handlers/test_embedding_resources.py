"""呼び出し単位の秘密情報・DB資源の組み立てを検証する。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr, ValidationError

from app.db import engine as engine_module
from app.db import iam
from app.lambda_handlers import embedding_resources as module
from app.lambda_handlers.settings import EmbeddingConsumerSettings


def settings(**overrides):
    return EmbeddingConsumerSettings(
        **{
            "env": "production",
            "database_url": "postgresql+asyncpg://vector_app@db.invalid:5432/vector?sslmode=require",
            "db_iam_auth": True,
            "aws_region": "ap-northeast-1",
            "gemini_api_key_parameter_path": (
                "/vector/embedding-consumer/gemini-api-key"
            ),
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"db_iam_auth": False},
        {"database_url": "postgresql+asyncpg://vector_app@db.invalid/vector"},
        {
            "database_url": "postgresql+asyncpg://vector_app:private@db.invalid/vector?sslmode=require"
        },
        {"aws_region": " "},
        {"gemini_api_key_parameter_path": " "},
    ],
)
def test_invalid_production_settings(overrides):
    with pytest.raises(ValidationError):
        settings(**overrides)


def test_settings_hide_url_and_do_not_load_dotenv():
    config = settings()
    assert config.model_config["env_file"] is None
    assert "db.invalid" not in repr(config)
    assert settings(env="test", db_iam_auth=False)


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
    monkeypatch.setattr(module, "create_embedding_consumer_engine", create)
    factory = Mock(return_value=Mock())
    monkeypatch.setattr(module, "caller_managed_session_factory", factory)
    monkeypatch.setattr(
        iam,
        "_rds_client",
        Mock(side_effect=AssertionError("global cache must not be used")),
    )
    return SimpleNamespace(
        rds=rds,
        session=session,
        secret=secret,
        engine=engine,
        create=create,
        factory=factory,
    )


@pytest.mark.asyncio
async def test_invocation_resources_and_injected_signer(mocks):
    config = settings()
    async with module.open_embedding_resources(config) as resources:
        assert resources.gemini_api_key.get_secret_value() == "private-key"
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
        region=config.aws_region, path=config.gemini_api_key_parameter_path
    )


@pytest.mark.asyncio
async def test_separate_scopes_refetch_and_recreate(mocks):
    mocks.secret.side_effect = [SecretStr("first"), SecretStr("second")]
    keys = []
    for _ in range(2):
        async with module.open_embedding_resources(settings()) as resources:
            keys.append(resources.gemini_api_key.get_secret_value())
    assert keys == ["first", "second"]
    assert mocks.secret.call_count == mocks.create.call_count == 2
    assert mocks.rds.close.call_count == mocks.engine.dispose.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["secret", "rds", "engine", "factory"])
async def test_partial_initialization_failure(mocks, phase):
    failure = RuntimeError("original")
    {
        "secret": mocks.secret,
        "rds": mocks.session.create_client,
        "engine": mocks.create,
        "factory": mocks.factory,
    }[phase].side_effect = failure
    with pytest.raises(RuntimeError) as caught:
        async with module.open_embedding_resources(settings()):
            pytest.fail("must not yield")
    assert caught.value is failure
    assert mocks.rds.close.call_count == (1 if phase in ("engine", "factory") else 0)
    assert mocks.engine.dispose.await_count == (1 if phase == "factory" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [None, RuntimeError("original"), asyncio.CancelledError()]
)
@pytest.mark.parametrize("log_fails", [False, True])
async def test_cleanup_preserves_result_and_exception(
    mocks, monkeypatch, failure, log_fails
):
    mocks.engine.dispose.side_effect = RuntimeError("private")
    mocks.rds.close.side_effect = RuntimeError("private")
    log = Mock(side_effect=RuntimeError("log") if log_fails else None)
    monkeypatch.setattr(module.logger, "warning", log)

    async def run():
        async with module.open_embedding_resources(settings()):
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
    mocks.engine.dispose.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        async with module.open_embedding_resources(settings()):
            pass
    mocks.rds.close.assert_called_once()


def test_engine_configuration_preserves_iam_tls(monkeypatch):
    create = Mock()
    monkeypatch.setattr(engine_module, "create_async_engine", create)
    provider = AsyncMock(return_value="token")
    engine_module.create_embedding_consumer_engine(
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
        == "vector-embedding-consumer"
    )
    assert "sslmode" not in create.call_args.args[0]
    with pytest.raises(ValueError):
        engine_module.create_embedding_consumer_engine(settings())


@pytest.mark.asyncio
async def test_cancelled_ssm_thread_closes_its_client(monkeypatch):
    import threading

    from app.aws import ssm

    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()
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
    create = Mock(side_effect=AssertionError("engine must not be created"))
    monkeypatch.setattr(module, "create_embedding_consumer_engine", create)

    async def run():
        async with module.open_embedding_resources(settings()):
            pytest.fail("must not yield")

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
    create.assert_not_called()
