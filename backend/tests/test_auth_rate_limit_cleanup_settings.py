"""掃除専用の接続先と認証境界を確認する。"""

import pytest
from pydantic import ValidationError

from app.lambda_handlers.auth_rate_limit_cleanup.settings import (
    AuthRateLimitCleanupSettings,
)


@pytest.mark.parametrize("username", ["vector", "vector_auth", "vector_app"])
def test_rejects_non_cleanup_user(username):
    """権限の広い既存ユーザーを掃除へ流用しない。"""
    with pytest.raises(ValidationError):
        AuthRateLimitCleanupSettings(
            env="test",
            database_url=f"postgresql+asyncpg://{username}@localhost/vector",
            aws_region="ap-northeast-1",
        )


def test_rejects_password_authentication():
    """IAM認証を無効化する設定を受け付けない。"""
    with pytest.raises(ValidationError):
        AuthRateLimitCleanupSettings(
            env="test",
            database_url="postgresql+asyncpg://vector_auth_rate_limit_cleanup@localhost/vector",
            aws_region="ap-northeast-1",
            db_iam_auth=False,
        )
