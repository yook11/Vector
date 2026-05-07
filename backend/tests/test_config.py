"""app.config.Settings のバリデータに関するユニットテスト。

PR8 (red-team S-SECRET-1 / S-AUTH-4 / C-CHAIN-D 防御):
- INTERNAL_API_SECRET の length / 弱秘密拒否 (既存挙動の回帰防止)
- DATABASE_URL の公開済 dev placeholder / 弱パスワード拒否
- 必須 settings (database_url / frontend_url / internal_frontend_base_url) が
  env 未設定なら起動時 ValidationError で fail-fast
- backend_url 死に変数の削除確認
"""

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings

_VALID_INTERNAL_SECRET = "a" * 64
_VALID_DATABASE_URL = "postgresql+asyncpg://vector_app:strongpassword@db:5432/vector"
_VALID_FRONTEND_URL = "https://app.example.com"
_VALID_INTERNAL_FRONTEND_BASE_URL = "http://frontend:3000"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """全 test で host の `.env` を遮断し、required env を baseline 値で埋める。

    Pydantic Settings の `model_config.env_file` は class 定義時に評価されるため
    モジュール変数 `_ENV_FILE` の monkeypatch だけでは効かない。`Settings.model_config`
    dict を直接書き換えて env_file fallback を nonexistent path に向ける。
    """
    nonexistent = tmp_path / "nonexistent.env"
    monkeypatch.setattr("app.config._ENV_FILE", nonexistent)
    monkeypatch.setitem(Settings.model_config, "env_file", str(nonexistent))
    monkeypatch.setenv("INTERNAL_API_SECRET", _VALID_INTERNAL_SECRET)
    monkeypatch.setenv("DATABASE_URL", _VALID_DATABASE_URL)
    monkeypatch.setenv("FRONTEND_URL", _VALID_FRONTEND_URL)
    monkeypatch.setenv("INTERNAL_FRONTEND_BASE_URL", _VALID_INTERNAL_FRONTEND_BASE_URL)


def _build_settings_with_secret(secret: str) -> Settings:
    """既存テスト互換: internal_api_secret だけ override して構築する。

    他の required field は autouse fixture `_isolate_env` が env で baseline を
    埋めているため、Pydantic Settings の env 経由 resolution で valid 値が入る。
    """
    return Settings(internal_api_secret=SecretStr(secret))


# --- INTERNAL_API_SECRET (既存挙動の回帰防止) -------------------------------


def test_strong_secret_is_accepted() -> None:
    """64 文字の hex は openssl rand -hex 32 の出力長で OK。"""
    secret = "a" * 64
    settings = _build_settings_with_secret(secret)
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
def test_known_weak_secret_is_rejected(weak: str) -> None:
    """既知の弱秘密は大文字小文字を問わず ValidationError で拒否される。"""
    with pytest.raises(ValidationError, match="known weak default"):
        _build_settings_with_secret(weak)


def test_short_secret_is_rejected() -> None:
    """32 文字未満は ValidationError で拒否される。"""
    with pytest.raises(ValidationError, match="at least 32 characters"):
        _build_settings_with_secret("a" * 31)


def test_boundary_length_secret_is_accepted() -> None:
    """ちょうど 32 文字は最低長を満たすので通る。"""
    secret = "a" * 32
    settings = _build_settings_with_secret(secret)
    assert settings.internal_api_secret.get_secret_value() == secret


# --- PR8: required URL settings の fail-fast --------------------------------


def test_settings_construct_with_all_required_env() -> None:
    """全 required env が valid なら Settings 構築が成功する (baseline)。"""
    s = Settings()
    assert s.database_url == _VALID_DATABASE_URL
    assert s.frontend_url == _VALID_FRONTEND_URL
    assert s.internal_frontend_base_url == _VALID_INTERNAL_FRONTEND_BASE_URL


@pytest.mark.parametrize(
    "missing_env",
    ["DATABASE_URL", "FRONTEND_URL", "INTERNAL_FRONTEND_BASE_URL"],
)
def test_settings_fail_fast_when_required_env_missing(
    monkeypatch: pytest.MonkeyPatch, missing_env: str
) -> None:
    """default 撤去した 3 settings のいずれか 1 つが未設定なら ValidationError。"""
    monkeypatch.delenv(missing_env, raising=False)
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert missing_env.lower() in str(exc_info.value).lower()


@pytest.mark.parametrize(
    "weak_url",
    [
        "postgresql+asyncpg://vector_app:vector_app@db:5432/vector",
        "postgresql+asyncpg://vector_app:<set-strong-password-here>@db:5432/vector",
    ],
)
def test_settings_reject_known_weak_database_url(
    monkeypatch: pytest.MonkeyPatch, weak_url: str
) -> None:
    """公開済 dev default / placeholder を含む DATABASE_URL は ValidationError。"""
    monkeypatch.setenv("DATABASE_URL", weak_url)
    with pytest.raises(ValidationError, match="dev placeholder/weak password"):
        Settings()


def test_settings_accept_ci_dummy_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI postgres service の汎用 dummy ``vector:vector`` は通る (本命は app role)。

    blocklist は application role 露出 (``vector_app:vector_app``) と placeholder
    残存に絞る。migration role の汎用 dev/CI password はノイズ過多のため検査しない。
    """
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://vector:vector@localhost:5432/vector"
    )
    s = Settings()
    assert "vector:vector" in s.database_url


def test_settings_no_longer_has_backend_url_field() -> None:
    """backend_url 死に変数が削除されていることを構造的に確認。"""
    s = Settings()
    assert not hasattr(s, "backend_url")
