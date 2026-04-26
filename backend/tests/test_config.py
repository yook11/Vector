"""app.config.Settings のバリデータに関するユニットテスト。

INTERNAL_API_SECRET の弱秘密拒否ロジックを直接検証する。
"""

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings


def _build_settings(secret: str) -> Settings:
    """テスト用に internal_api_secret のみを差し替えて Settings を構築する。"""
    return Settings(internal_api_secret=SecretStr(secret))


def test_strong_secret_is_accepted() -> None:
    """64 文字の hex は openssl rand -hex 32 の出力長で OK。"""
    secret = "a" * 64
    settings = _build_settings(secret)
    assert settings.internal_api_secret.get_secret_value() == secret


@pytest.mark.parametrize(
    "weak",
    [
        "change-me-in-production",
        "change-me",
        "changeme",
        "secret",
        "password",
        "CHANGE-ME-IN-PRODUCTION",
    ],
)
def test_known_weak_default_is_rejected(weak: str) -> None:
    """既知の弱秘密は大文字小文字を問わず ValidationError で拒否される。"""
    with pytest.raises(ValidationError, match="known weak default"):
        _build_settings(weak)


def test_short_secret_is_rejected() -> None:
    """32 文字未満は ValidationError で拒否される。"""
    with pytest.raises(ValidationError, match="at least 32 characters"):
        _build_settings("a" * 31)


def test_boundary_length_is_accepted() -> None:
    """ちょうど 32 文字は最低長を満たすので通る。"""
    secret = "a" * 32
    settings = _build_settings(secret)
    assert settings.internal_api_secret.get_secret_value() == secret
