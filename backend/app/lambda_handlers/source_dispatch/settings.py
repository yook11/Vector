"""取得依頼の投入に必要なDB・AWS接続設定。"""

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

from app.db.settings import DatabaseConnectionSettings


class SourceDispatchSettings(DatabaseConnectionSettings):
    model_config = SettingsConfigDict(env_file=None, hide_input_in_errors=True)

    env: Literal["development", "test", "production"] = "production"
    database_url: str = Field(repr=False)
    db_iam_auth: bool = True
    aws_region: str = Field(min_length=1)
    sqs_source_acquisition_queue_url: str = Field(min_length=1)

    @field_validator("aws_region", "sqs_source_acquisition_queue_url")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("connection setting must not be blank")
        return value

    @model_validator(mode="after")
    def _require_iam(self) -> Self:
        if not self.db_iam_auth:
            raise ValueError("Source dispatch requires RDS IAM authentication")
        return self
