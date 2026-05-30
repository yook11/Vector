"""app.config.Settings のバリデータに関するユニットテスト。

必須設定の fail-fast、公開済み placeholder の拒否、内部 secret の強度と
宛先 allowlist を検証する。
"""

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings

_VALID_BFF_SECRET = "b" * 64
_VALID_REVALIDATE_SECRET = "c" * 64
_VALID_DATABASE_URL = "postgresql+asyncpg://vector_app:strongpassword@db:5432/vector"
_VALID_FRONTEND_URL = "https://app.example.com"
_VALID_INTERNAL_FRONTEND_BASE_URL = "http://frontend:3000"

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


# --- 必須 URL settings の fail-fast -----------------------------------------


def test_settings_construct_with_all_required_env() -> None:
    """全 required env が valid なら Settings 構築が成功する (baseline)。"""
    s = Settings()
    assert s.database_url == _VALID_DATABASE_URL
    assert s.frontend_url == _VALID_FRONTEND_URL
    assert s.internal_frontend_base_url == _VALID_INTERNAL_FRONTEND_BASE_URL


@pytest.mark.parametrize(
    "missing_env",
    [
        "DATABASE_URL",
        "FRONTEND_URL",
        "INTERNAL_FRONTEND_BASE_URL",
        "BFF_JWT_SIGNING_SECRET",
        "REVALIDATE_BEARER_SECRET",
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


# --- 内部 secret の強度検査 / 同一値拒否 ------------------------------------


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


# --- internal_frontend_base_url の宛先 allowlist -----------------------------
#
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
        "http://your-vector-frontend-app.flycast.attacker.com:3000",  # suffix が末尾でない
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
