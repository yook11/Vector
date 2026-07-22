"""app.config.Settings のバリデータに関するユニットテスト。

必須設定の fail-fast、公開済み placeholder の拒否、内部 secret の強度と
宛先 allowlist を検証する。
"""

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings

_VALID_BFF_SECRET = "b" * 64
_VALID_REVALIDATE_SECRET = "c" * 64
# baseline は sslmode=require 付き。production SSL fail-safe
# (_require_ssl_in_production) は env="production" のとき DB URL に TLS sslmode を
# 要求するため、production を渡す既存テスト (flycast narrowing 等) がこの fixture を
# 流用しても先に SSL で落ちない。dev では sslmode は無視されるので harmless。
_VALID_DATABASE_URL = (
    "postgresql+asyncpg://vector_app:strongpassword@db:5432/vector?sslmode=require"
)
_VALID_FRONTEND_URL = "https://app.example.com"
_VALID_INTERNAL_FRONTEND_BASE_URL = "http://frontend:3000"
_VALID_CROSSREF_CONTACT_EMAIL = "crossref-contact@portfolio.dev"
_CI_CROSSREF_CONTACT_EMAIL = "crossref-contact@example.invalid"
_DATABASE_PASSWORD_SENTINEL = "vector_auth-SETTINGS-PASSWORD-MUST-NOT-LEAK"

# 強度テストで parametrize する内部 secret の field 名。
_NEW_SECRET_FIELD_NAMES = ["bff_jwt_signing_secret", "revalidate_bearer_secret"]


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
    monkeypatch.setenv("DATABASE_URL", _VALID_DATABASE_URL)
    monkeypatch.setenv("FRONTEND_URL", _VALID_FRONTEND_URL)
    monkeypatch.setenv("INTERNAL_FRONTEND_BASE_URL", _VALID_INTERNAL_FRONTEND_BASE_URL)
    # baseline は両 secret を valid な別値で設定する。
    monkeypatch.setenv("BFF_JWT_SIGNING_SECRET", _VALID_BFF_SECRET)
    monkeypatch.setenv("REVALIDATE_BEARER_SECRET", _VALID_REVALIDATE_SECRET)
    monkeypatch.setenv("CROSSREF_CONTACT_EMAIL", _VALID_CROSSREF_CONTACT_EMAIL)


def test_settings_construct_with_all_required_env() -> None:
    """全 required env が valid なら Settings 構築が成功する (baseline)。"""
    s = Settings()
    assert s.database_url == _VALID_DATABASE_URL
    assert s.frontend_url == _VALID_FRONTEND_URL
    assert s.internal_frontend_base_url == _VALID_INTERNAL_FRONTEND_BASE_URL
    assert str(s.crossref_contact_email) == _VALID_CROSSREF_CONTACT_EMAIL


@pytest.mark.parametrize(
    "missing_env",
    [
        "DATABASE_URL",
        "FRONTEND_URL",
        "INTERNAL_FRONTEND_BASE_URL",
        "BFF_JWT_SIGNING_SECRET",
        "REVALIDATE_BEARER_SECRET",
        "CROSSREF_CONTACT_EMAIL",
    ],
)
def test_settings_fail_fast_when_required_env_missing(
    monkeypatch: pytest.MonkeyPatch, missing_env: str
) -> None:
    """default の無い required settings のいずれか 1 つが未設定なら ValidationError。"""
    monkeypatch.delenv(missing_env, raising=False)
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert missing_env.lower() in str(exc_info.value).lower()


def test_settings_accepts_crossref_ci_dummy() -> None:
    """外部通信しないCI/testでは予約ドメインのdummyを許可する。"""
    s = Settings(crossref_contact_email=_CI_CROSSREF_CONTACT_EMAIL)
    assert s.crossref_contact_email == _CI_CROSSREF_CONTACT_EMAIL


def test_settings_rejects_malformed_crossref_contact_email() -> None:
    """User-Agent headerを壊す改行入り連絡先は起動時に拒否する。"""
    with pytest.raises(ValidationError, match="crossref_contact_email"):
        Settings(crossref_contact_email="contact@example.com\r\nX-Test: injected")


def test_settings_rejects_crossref_ci_dummy_in_production() -> None:
    """productionでは連絡不能なCI dummyを拒否する。"""
    with pytest.raises(ValidationError, match="monitored alias"):
        Settings(
            env="production",
            internal_frontend_base_url="http://your-vector-frontend-app.flycast:3000",
            crossref_contact_email=_CI_CROSSREF_CONTACT_EMAIL,
        )


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


@pytest.mark.parametrize(
    ("env_name", "weak_url"),
    [
        (
            "MIGRATION_DATABASE_URL",
            "postgresql+asyncpg://vector:<set-strong-password-here>@db:5432/vector",
        ),
        (
            "AUTH_RETENTION_DATABASE_URL",
            "postgresql+asyncpg://vector_auth:vector_auth@db:5432/vector",
        ),
        (
            "AUTH_RETENTION_DATABASE_URL",
            "postgresql+asyncpg://vector_auth:<set-strong-password-here>@db:5432/vector",
        ),
    ],
)
def test_settings_reject_known_weak_optional_database_urls(
    monkeypatch: pytest.MonkeyPatch, env_name: str, weak_url: str
) -> None:
    """任意 DB URL でも公開済 dev default / placeholder は ValidationError。"""
    monkeypatch.setenv(env_name, weak_url)
    with pytest.raises(ValidationError, match=env_name):
        Settings()


def test_auth_retention_validation_error_does_not_expose_database_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """起動時の弱URL拒否でもcredentialをValidationError表示へ出さない。"""
    raw_url = (
        f"postgresql+asyncpg://vector_auth:{_DATABASE_PASSWORD_SENTINEL}@db:5432/vector"
    )
    monkeypatch.setenv("AUTH_RETENTION_DATABASE_URL", raw_url)

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    for rendered in (str(exc_info.value), repr(exc_info.value)):
        assert raw_url not in rendered
        assert _DATABASE_PASSWORD_SENTINEL not in rendered
        assert "input_value" not in rendered


def test_settings_accept_ci_dummy_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI postgres service の汎用 dummy ``vector:vector`` は許可する。

    blocklist は application role 露出と placeholder 残存に絞る。
    """
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://vector:vector@localhost:5432/vector"
    )
    s = Settings()
    assert "vector:vector" in s.database_url


def test_settings_no_longer_has_backend_url_field() -> None:
    """Settings が backend_url field を持たないことを確認。"""
    s = Settings()
    assert not hasattr(s, "backend_url")


def test_strong_new_secrets_are_accepted() -> None:
    """64 文字 hex (openssl rand -hex 32 の出力長) の新 secret は両方 OK。"""
    s = Settings()
    assert s.bff_jwt_signing_secret.get_secret_value() == _VALID_BFF_SECRET
    assert s.revalidate_bearer_secret.get_secret_value() == _VALID_REVALIDATE_SECRET


@pytest.mark.parametrize("field_name", _NEW_SECRET_FIELD_NAMES)
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
def test_known_weak_new_secret_is_rejected(field_name: str, weak: str) -> None:
    """既知の弱秘密は大文字小文字を問わず ValidationError で拒否される。"""
    with pytest.raises(ValidationError, match="known weak default"):
        Settings(**{field_name: SecretStr(weak)})


@pytest.mark.parametrize("field_name", _NEW_SECRET_FIELD_NAMES)
def test_short_new_secret_is_rejected(field_name: str) -> None:
    """32 文字未満の新 secret は ValidationError で拒否される。"""
    with pytest.raises(ValidationError, match="at least 32 characters"):
        Settings(**{field_name: SecretStr("a" * 31)})


@pytest.mark.parametrize("field_name", _NEW_SECRET_FIELD_NAMES)
def test_boundary_length_new_secret_is_accepted(field_name: str) -> None:
    """ちょうど 32 文字の新 secret は最低長を満たすので通る。"""
    # baseline (env) の相手 secret は 64 文字なので同一値拒否には掛からない。
    s = Settings(**{field_name: SecretStr("d" * 32)})
    assert getattr(s, field_name).get_secret_value() == "d" * 32


def test_reject_when_secrets_equal() -> None:
    """両 secret が同一値なら構造分離が無効化されるため拒否。"""
    with pytest.raises(ValidationError, match="must differ"):
        Settings(
            bff_jwt_signing_secret=SecretStr(_VALID_BFF_SECRET),
            revalidate_bearer_secret=SecretStr(_VALID_BFF_SECRET),
        )


# notifier (FrontendRevalidateNotifier) は SSRF guard をバイパスして
# REVALIDATE_BEARER_SECRET を Bearer 送信するため、宛先が攻撃者制御に向くと
# secret 持ち出し経路になる。

_VALID_FLYCAST_URL = "http://your-vector-frontend-app.flycast:3000"

# 本番では reject されるが development では許可される dev host 群。
_DEV_HOST_URLS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://frontend:3000",
]


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://attacker.example.com",
        "https://evil.com",
        "http://169.254.169.254",
        "http://frontend.attacker.com",  # substring 混同 (frontend で始まる別ホスト)
        "http://xflycast:3000",  # suffix の前に dot が無い
        # flycast suffix がホスト末尾でない。
        "http://your-vector-frontend-app.flycast.attacker.com:3000",
    ],
)
def test_internal_frontend_base_url_rejects_external_host(bad_url: str) -> None:
    """allowlist 外ホストは全環境で reject (naive な substring 検査ではない)。"""
    with pytest.raises(ValidationError, match="INTERNAL_FRONTEND_BASE_URL"):
        Settings(internal_frontend_base_url=bad_url)


@pytest.mark.parametrize("bad_url", ["gopher://frontend:3000", "file:///etc/passwd"])
def test_internal_frontend_base_url_rejects_non_http_scheme(bad_url: str) -> None:
    """http / https 以外の scheme は ValidationError。"""
    with pytest.raises(ValidationError, match="INTERNAL_FRONTEND_BASE_URL"):
        Settings(internal_frontend_base_url=bad_url)


def test_internal_frontend_base_url_accepts_flycast_in_development() -> None:
    """development でも *.flycast は global allowlist で許可される。"""
    s = Settings(internal_frontend_base_url=_VALID_FLYCAST_URL)
    assert s.internal_frontend_base_url == _VALID_FLYCAST_URL


@pytest.mark.parametrize("dev_host_url", _DEV_HOST_URLS)
def test_internal_frontend_base_url_accepts_dev_host_in_development(
    dev_host_url: str,
) -> None:
    """development では dev host を許可 (compose / CI 互換)。"""
    s = Settings(internal_frontend_base_url=dev_host_url)
    assert s.internal_frontend_base_url == dev_host_url


@pytest.mark.parametrize("dev_host_url", _DEV_HOST_URLS)
def test_internal_frontend_base_url_rejects_dev_host_in_production(
    dev_host_url: str,
) -> None:
    """production では dev host は ValidationError (*.flycast のみ許可)。"""
    with pytest.raises(ValidationError, match="production"):
        Settings(env="production", internal_frontend_base_url=dev_host_url)


def test_internal_frontend_base_url_accepts_flycast_in_production() -> None:
    """production で *.flycast は許可される。"""
    s = Settings(env="production", internal_frontend_base_url=_VALID_FLYCAST_URL)
    assert s.internal_frontend_base_url == _VALID_FLYCAST_URL


# Neon は public internet 越しの接続のため、production では DB 接続文字列に TLS
# sslmode (require / verify-ca / verify-full) を要求する。dev は docker 同一
# network の平文で良いので何も強制しない。

# sslmode を持たない Neon 風 URL。各テストで sslmode を付け外しする土台。
_NEON_DB_URL_NO_SSL = (
    "postgresql+asyncpg://vector_app:strongpassword@ep-x.neon.tech/neondb"
)
_NEON_AUTH_RETENTION_DB_URL_NO_SSL = (
    "postgresql+asyncpg://vector_auth:strongpassword@ep-x.neon.tech/neondb"
)


def test_production_rejects_database_url_without_sslmode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """production で DATABASE_URL に sslmode が無ければ ValidationError。"""
    monkeypatch.setenv("DATABASE_URL", _NEON_DB_URL_NO_SSL)
    with pytest.raises(ValidationError, match="sslmode"):
        Settings(env="production", internal_frontend_base_url=_VALID_FLYCAST_URL)


def test_production_accepts_database_url_with_sslmode_require(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """production で sslmode=require 付き DATABASE_URL は通る。"""
    monkeypatch.setenv("DATABASE_URL", f"{_NEON_DB_URL_NO_SSL}?sslmode=require")
    s = Settings(env="production", internal_frontend_base_url=_VALID_FLYCAST_URL)
    assert "sslmode=require" in s.database_url


def test_production_rejects_database_url_with_sslmode_disable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """production で sslmode=disable (平文) は ValidationError。"""
    monkeypatch.setenv("DATABASE_URL", f"{_NEON_DB_URL_NO_SSL}?sslmode=disable")
    with pytest.raises(ValidationError, match="sslmode"):
        Settings(env="production", internal_frontend_base_url=_VALID_FLYCAST_URL)


def test_production_rejects_migration_url_without_sslmode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """production で MIGRATION_DATABASE_URL に sslmode が無ければ ValidationError。"""
    # DATABASE_URL 側は TLS を満たし、MIGRATION_DATABASE_URL だけ平文にする。
    monkeypatch.setenv("DATABASE_URL", f"{_NEON_DB_URL_NO_SSL}?sslmode=require")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", _NEON_DB_URL_NO_SSL)
    with pytest.raises(ValidationError, match="MIGRATION_DATABASE_URL"):
        Settings(env="production", internal_frontend_base_url=_VALID_FLYCAST_URL)


def test_production_rejects_auth_retention_url_without_sslmode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """production で AUTH_RETENTION_DATABASE_URL に sslmode が無ければ reject。"""
    monkeypatch.setenv("DATABASE_URL", f"{_NEON_DB_URL_NO_SSL}?sslmode=require")
    monkeypatch.setenv(
        "AUTH_RETENTION_DATABASE_URL", _NEON_AUTH_RETENTION_DB_URL_NO_SSL
    )
    with pytest.raises(ValidationError, match="AUTH_RETENTION_DATABASE_URL"):
        Settings(env="production", internal_frontend_base_url=_VALID_FLYCAST_URL)


def test_production_accepts_auth_retention_url_with_sslmode_require(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """production で auth retention 用 URL も sslmode=require 付きなら通る。"""
    auth_url = f"{_NEON_AUTH_RETENTION_DB_URL_NO_SSL}?sslmode=require"
    monkeypatch.setenv("DATABASE_URL", f"{_NEON_DB_URL_NO_SSL}?sslmode=require")
    monkeypatch.setenv("AUTH_RETENTION_DATABASE_URL", auth_url)
    s = Settings(env="production", internal_frontend_base_url=_VALID_FLYCAST_URL)
    assert s.auth_retention_database_url == auth_url


def test_development_allows_database_url_without_sslmode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """development では sslmode 無し DATABASE_URL でも起動できる (docker 平文)。"""
    monkeypatch.setenv("DATABASE_URL", _NEON_DB_URL_NO_SSL)
    s = Settings()
    assert s.database_url == _NEON_DB_URL_NO_SSL


# postgres_collect_password は本番 runtime では単体 password としては読まず、
# test_db_user_isolation の guard 統合テストが os.environ 直読みを避けて settings
# 経由で vector_collect 接続を張るためだけに存在する。未設定なら None で guard が
# skip し、設定時は SecretStr として読める、という 2 つの不変条件を固定する。


def test_postgres_collect_password_defaults_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSTGRES_COLLECT_PASSWORD 未設定なら None (guard 統合テストが skip する条件)。"""
    monkeypatch.delenv("POSTGRES_COLLECT_PASSWORD", raising=False)
    s = Settings()
    assert s.postgres_collect_password is None


def test_postgres_collect_password_loaded_as_secretstr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """設定時は SecretStr として読め、get_secret_value で元値を取り出せる。"""
    monkeypatch.setenv("POSTGRES_COLLECT_PASSWORD", "test-collect-password")
    s = Settings()
    assert isinstance(s.postgres_collect_password, SecretStr)
    assert s.postgres_collect_password.get_secret_value() == "test-collect-password"


def test_tavily_api_key_defaults_to_empty_secretstr() -> None:
    """TAVILY_API_KEY 未設定なら空 SecretStr。provider 側が fail-fast する。"""
    s = Settings()
    assert isinstance(s.tavily_api_key, SecretStr)
    assert s.tavily_api_key.get_secret_value() == ""


def test_tavily_api_key_loaded_as_secretstr(monkeypatch: pytest.MonkeyPatch) -> None:
    """設定時は SecretStr として読め、値は settings 経由で provider に渡せる。"""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")
    s = Settings()
    assert isinstance(s.tavily_api_key, SecretStr)
    assert s.tavily_api_key.get_secret_value() == "tvly-test-key"
