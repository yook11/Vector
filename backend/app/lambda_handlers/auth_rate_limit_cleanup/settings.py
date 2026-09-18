"""掃除専用のDB接続設定。"""

from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import SettingsConfigDict
from sqlalchemy.engine import make_url

from app.db.settings import DatabaseConnectionSettings


class AuthRateLimitCleanupSettings(DatabaseConnectionSettings):
    """認証カウンターだけを削除できるIAMユーザーへ接続する。"""

    model_config = SettingsConfigDict(env_file=None, hide_input_in_errors=True)

    env: Literal["production", "test"] = "production"
    database_url: str = Field(repr=False)
    db_iam_auth: bool = True
    aws_region: str = Field(min_length=1)

    @model_validator(mode="after")
    def _require_cleanup_user(self) -> Self:
        if not self.db_iam_auth:
            raise ValueError("cleanup requires RDS IAM authentication")
        if make_url(self.database_url).username != "vector_auth_rate_limit_cleanup":
            raise ValueError("cleanup requires its dedicated database user")
        if not self.aws_region.strip():
            raise ValueError("AWS region must not be blank")
        return self
