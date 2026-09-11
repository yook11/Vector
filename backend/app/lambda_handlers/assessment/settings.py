from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

from app.db.settings import DatabaseConnectionSettings


class AssessmentConsumerSettings(DatabaseConnectionSettings):
    """Consumerの秘密情報取得先とDB接続設定だけを読み込む。"""

    model_config = SettingsConfigDict(env_file=None, hide_input_in_errors=True)

    env: Literal["development", "test", "production"] = "production"
    aws_region: str = Field(min_length=1)
    database_url: str = Field(repr=False)
    db_iam_auth: bool = True
    deepseek_api_key_parameter_path: str = Field(min_length=1)

    @field_validator("aws_region", "deepseek_api_key_parameter_path")
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
