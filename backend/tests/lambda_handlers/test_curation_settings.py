"""工程別の設定検証とEngineへの接続設定を確認する。"""

from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.db import engine as engine_module
from app.lambda_handlers.curation.settings import CurationConsumerSettings


def settings(**overrides):
    return CurationConsumerSettings(
        **{
            "env": "production",
            "database_url": "postgresql+asyncpg://vector_app@db.invalid:5432/vector?sslmode=require",
            "db_iam_auth": True,
            "aws_region": "ap-northeast-1",
            "gemini_api_key_parameter_path": (
                "/vector/curation-consumer/gemini-api-key"
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
        {"gemini_api_key_parameter_path": " "},
    ],
)
def test_invalid_production_settings(overrides):
    """IAM無効化は環境を問わず拒否し、本番のTLSと設定項目も検証する。"""
    with pytest.raises(ValidationError):
        settings(**overrides)


def test_settings_hide_url_and_do_not_load_dotenv():
    """dotenvを読まず接続情報を表示せず、テスト環境でもIAMを必須にする。"""
    config = settings()
    assert config.model_config["env_file"] is None
    assert "db.invalid" not in repr(config)
    with pytest.raises(ValidationError):
        settings(env="test", db_iam_auth=False)


def test_engine_configuration_preserves_iam_tls(monkeypatch):
    """IAM署名器の必須性と、Engineへ渡す接続数・待機上限・TLS設定を確認する。"""
    create = Mock()
    monkeypatch.setattr(engine_module, "create_async_engine", create)
    provider = AsyncMock(return_value="token")
    engine_module.create_curation_consumer_engine(
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
        == "vector-curation-consumer"
    )
    assert "sslmode" not in create.call_args.args[0]
    with pytest.raises(TypeError):
        engine_module.create_curation_consumer_engine(settings())
    with pytest.raises(TypeError):
        engine_module.create_curation_consumer_engine(
            settings(), password_provider=None
        )
