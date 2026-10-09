from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.db.settings import DatabaseConnectionSettings


class AssessmentNotificationSettings(BaseSettings):
    """保存後通知に必要な内部宛先と既存の認証キー取得先を読む。"""

    model_config = SettingsConfigDict(env_file=None, hide_input_in_errors=True)

    internal_frontend_base_url: str
    revalidate_bearer_secret_parameter_path: str


class AssessmentConsumerSettings(DatabaseConnectionSettings):
    """Consumerの秘密情報取得先とDB接続設定だけを読み込む。"""

    model_config = SettingsConfigDict(env_file=None, hide_input_in_errors=True)

    env: Literal["development", "test", "production"] = "production"
    aws_region: str = Field(min_length=1)
    database_url: str = Field(repr=False)
    db_iam_auth: bool = True
    gemini_api_key_parameter_path: str = Field(min_length=1)

    @field_validator("aws_region", "gemini_api_key_parameter_path")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("connection setting must not be blank")
        return value

    @model_validator(mode="after")
    def _require_iam(self) -> Self:
        if not self.db_iam_auth:
            raise ValueError("Consumer requires RDS IAM authentication")
        return self


@dataclass(frozen=True, slots=True)
class AssessmentLambdaSettings:
    """Consumerと保存後通知の設定を、Lambda起動時の1回の読み込みとして扱う。"""

    consumer: AssessmentConsumerSettings
    notification: AssessmentNotificationSettings

    @property
    def env(self) -> str:
        return self.consumer.env

    @classmethod
    def load(cls) -> Self:
        return cls(
            consumer=AssessmentConsumerSettings(),  # type: ignore[call-arg]
            notification=AssessmentNotificationSettings(),  # type: ignore[call-arg]
        )
