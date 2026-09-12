"""工程別の設定検証とEngineへの接続設定を確認する。"""

from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.db import engine as engine_module
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
