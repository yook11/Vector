"""記事取得の起動に必要なDBとAWSの設定。"""

from typing import Literal, Self

from pydantic import EmailStr, Field, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

from app.db.settings import DatabaseConnectionSettings


class AcquisitionConsumerSettings(DatabaseConnectionSettings):
    """アプリ全体の設定を読まず、既存のIAM・TLS条件を維持する。"""

    model_config = SettingsConfigDict(env_file=None, hide_input_in_errors=True)

    env: Literal["development", "test", "production"] = "production"
    aws_region: str = Field(min_length=1)
    database_url: str = Field(repr=False)
    db_iam_auth: bool = True
    crossref_contact_email: EmailStr | Literal["crossref-contact@example.invalid"]

    @field_validator("aws_region")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("connection setting must not be blank")
        return value

    @model_validator(mode="after")
    def _require_iam(self) -> Self:
        if not self.db_iam_auth:
            raise ValueError("Consumer requires RDS IAM authentication")
        if (
            self.env == "production"
            and self.crossref_contact_email == "crossref-contact@example.invalid"
        ):
            raise ValueError("production requires a monitored Crossref contact")
        return self
