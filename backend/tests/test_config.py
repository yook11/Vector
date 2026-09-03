"""app.config.Settings のバリデータに関するユニットテスト。

必須設定の fail-fast、公開済み placeholder の拒否、内部 secret の強度と
宛先 allowlist を検証する。
"""

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings
from app.db.settings import DatabaseSettings

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
    # host の AWS_REGION を遮断する (IAM 認証の要求検証を host 環境に依存させない)。
    monkeypatch.delenv("AWS_REGION", raising=False)


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


def test_database_settings_validation_error_hides_database_url() -> None:
    """DB設定基底を直接使ってもURL内のcredentialを例外表示へ出さない。"""
    raw_url = (
        "postgresql+asyncpg://vector_app:"
        f"vector_app-{_DATABASE_PASSWORD_SENTINEL}@db:5432/vector"
    )

    with pytest.raises(ValidationError) as exc_info:
        DatabaseSettings(database_url=raw_url)

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

# 実行基盤の内部 namespace。Fly と AWS の両方を同じ allowlist が受理する。
# frontend 側 (internal-config.test.ts) が同じ 2 つを同形で固定している。
_INTERNAL_NAMESPACE_URLS = [
    _VALID_FLYCAST_URL,
    "http://frontend.vector.internal:3000",
]

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
        "http://evilvector.internal:3000",  # suffix の前に dot が無い (AWS 側)
        # namespace suffix がホスト末尾でない。
        "http://frontend.vector.internal.attacker.com:3000",
        # .internal だが自分の namespace ではない (許すのは TLD 全体ではない)。
        "http://frontend.attacker.internal:3000",
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


@pytest.mark.parametrize("namespace_url", _INTERNAL_NAMESPACE_URLS)
def test_internal_frontend_base_url_accepts_internal_namespace_in_development(
    namespace_url: str,
) -> None:
    """development でも内部 namespace は global allowlist で許可される。"""
    s = Settings(internal_frontend_base_url=namespace_url)
    assert s.internal_frontend_base_url == namespace_url


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
    """production では dev host は ValidationError (内部 namespace のみ許可)。"""
    with pytest.raises(ValidationError, match="production"):
        Settings(env="production", internal_frontend_base_url=dev_host_url)


@pytest.mark.parametrize("namespace_url", _INTERNAL_NAMESPACE_URLS)
def test_internal_frontend_base_url_accepts_internal_namespace_in_production(
    namespace_url: str,
) -> None:
    """production で内部 namespace は許可される。"""
    s = Settings(env="production", internal_frontend_base_url=namespace_url)
    assert s.internal_frontend_base_url == namespace_url


# egress proxy の宛先。この値は全 task の全外向き通信の経路になるため、攻撃者ホストに
# 向いた場合の射程は revalidate 宛先より広い (平文 http の上流では header ごと通る)。
# dev host を許さない点だけが internal_frontend_base_url と違う: proxy は AWS にしか
# 存在せず、他環境では未設定が正しい状態。

_VALID_EGRESS_PROXY_URLS = [
    # Terraform が実際に注入する値 (locals.tf の proxy_url =
    # "http://proxy.${internal_namespace}:${proxy_port}")。両者がずれたら
    # task が起動できなくなるので、実値をそのまま受理側で固定する。
    "http://proxy.vector.internal:3128",
    "http://proxy.your-vector-app.flycast:3128",
]


def test_egress_proxy_url_defaults_to_none() -> None:
    """未設定なら None (直接接続のまま = Fly / compose の既定)。"""
    assert Settings().egress_proxy_url is None


@pytest.mark.parametrize("proxy_url", _VALID_EGRESS_PROXY_URLS)
def test_egress_proxy_url_accepts_internal_namespace(proxy_url: str) -> None:
    s = Settings(egress_proxy_url=proxy_url)
    assert s.egress_proxy_url == proxy_url


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://attacker.example.com:3128",
        "https://evil.com",
        "http://169.254.169.254:3128",
        "http://evilvector.internal:3128",  # suffix の前に dot が無い
        "http://proxy.vector.internal.attacker.com:3128",  # 末尾でない
        "http://proxy.attacker.internal:3128",  # 別 namespace
    ],
)
def test_egress_proxy_url_rejects_non_internal_host(bad_url: str) -> None:
    with pytest.raises(ValidationError, match="EGRESS_PROXY_URL"):
        Settings(egress_proxy_url=bad_url)


@pytest.mark.parametrize("dev_url", ["http://localhost:3128", "http://127.0.0.1:3128"])
def test_egress_proxy_url_rejects_dev_host(dev_url: str) -> None:
    """dev host は全環境で拒否 (proxy を使う環境は AWS だけ)。"""
    with pytest.raises(ValidationError, match="EGRESS_PROXY_URL"):
        Settings(egress_proxy_url=dev_url)


@pytest.mark.parametrize(
    "bad_url", ["socks5://proxy.vector.internal:1080", "file:///x"]
)
def test_egress_proxy_url_rejects_non_http_scheme(bad_url: str) -> None:
    with pytest.raises(ValidationError, match="EGRESS_PROXY_URL"):
        Settings(egress_proxy_url=bad_url)


# RDS IAM 認証。射程は app runtime の接続だけで、migration は migrator role の
# password 認証のまま (master は break-glass 専用という以前の決定と整合)。
# 有効時に URL の password は provider に上書きされて黙って無視されるので起動時に弾く。

_IAM_RUNTIME_URL = (
    "postgresql+asyncpg://vector_app@"
    "vector-db.abc.ap-northeast-1.rds.amazonaws.com:5432/vector?sslmode=require"
)


def test_db_iam_auth_defaults_to_false() -> None:
    """既定は無効。Fly は URL の password で繋ぐ。"""
    assert Settings().db_iam_auth is False


def test_db_iam_auth_accepts_passwordless_runtime_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", _IAM_RUNTIME_URL)
    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
    assert Settings(db_iam_auth=True).db_iam_auth is True


def test_db_iam_auth_requires_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """region 無しに token は署名できない。

    botocore が region に使う env は AWS_DEFAULT_REGION だけで、ECS が注入する
    AWS_REGION は読まない。解決規則に任せると本番の全 task が engine 生成で
    NoRegionError になるため、settings で受けて起動時に要求する。
    """
    monkeypatch.setenv("DATABASE_URL", _IAM_RUNTIME_URL)
    monkeypatch.delenv("AWS_REGION", raising=False)
    with pytest.raises(ValidationError, match="AWS_REGION"):
        Settings(db_iam_auth=True)


def test_aws_region_defaults_to_none() -> None:
    """IAM 認証を使わない環境 (Fly / dev) では未設定が正しい。"""
    assert Settings().aws_region is None


@pytest.mark.parametrize("env_name", ["DATABASE_URL", "AUTH_RETENTION_DATABASE_URL"])
def test_db_iam_auth_rejects_password_in_runtime_url(
    monkeypatch: pytest.MonkeyPatch, env_name: str
) -> None:
    """password が残っていると「IAM のつもりで password 認証」を疑えなくなる。"""
    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
    monkeypatch.setenv("DATABASE_URL", _IAM_RUNTIME_URL)
    monkeypatch.setenv(
        env_name, _IAM_RUNTIME_URL.replace("vector_app@", "vector_app:leftover@")
    )
    with pytest.raises(ValidationError, match="DB_IAM_AUTH"):
        Settings(db_iam_auth=True)


def test_db_iam_auth_allows_password_in_migration_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """migration は射程外。migrator role は password 認証を続ける。"""
    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
    monkeypatch.setenv("DATABASE_URL", _IAM_RUNTIME_URL)
    monkeypatch.setenv(
        "MIGRATION_DATABASE_URL",
        "postgresql+asyncpg://vector:migratorsecret@db:5432/vector?sslmode=require",
    )
    assert Settings(db_iam_auth=True).migration_database_url is not None


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


# 外部検索の宛先。内部宛 client は SSRF 検証を通さないため、宛先が正しいことの
# 根拠はこの validator にしかない。内部 namespace の suffix は使えない: gateway の
# host は *.gateway.bedrock-agentcore.<region>.amazonaws.com で、AWS 所有の
# 公開ドメインに属する。

_VALID_AGENTCORE_GATEWAY_URLS = [
    # Terraform が注入する実値の形 (aws_bedrockagentcore_gateway.gateway_url)。
    "https://vector-web-search-abc123.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com",
    "https://gw.gateway.bedrock-agentcore.us-east-1.amazonaws.com",
]


def test_agentcore_gateway_url_defaults_to_none() -> None:
    """未設定なら None。外部検索を持たない段では未設定が正しい状態。"""
    assert Settings().agentcore_gateway_url is None


@pytest.mark.parametrize("gateway_url", _VALID_AGENTCORE_GATEWAY_URLS)
def test_agentcore_gateway_url_accepts_aws_owned_https_host(gateway_url: str) -> None:
    s = Settings(agentcore_gateway_url=gateway_url, aws_region="ap-northeast-1")
    assert s.agentcore_gateway_url == gateway_url


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://attacker.example.com/mcp",
        "https://amazonaws.com.attacker.example/mcp",  # 末尾でない
        "https://evilamazonaws.com/mcp",  # suffix の前に dot が無い
        "https://169.254.170.2/mcp",  # IP リテラル
        "https:///mcp",  # host が無い
    ],
)
def test_agentcore_gateway_url_rejects_non_aws_host(bad_url: str) -> None:
    with pytest.raises(ValidationError, match="AGENTCORE_GATEWAY_URL"):
        Settings(agentcore_gateway_url=bad_url, aws_region="ap-northeast-1")


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://gw.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com",
        "file:///etc/passwd",
    ],
)
def test_agentcore_gateway_url_rejects_non_https_scheme(bad_url: str) -> None:
    """SigV4 署名済みリクエストを平文で流させない。"""
    with pytest.raises(ValidationError, match="AGENTCORE_GATEWAY_URL"):
        Settings(agentcore_gateway_url=bad_url, aws_region="ap-northeast-1")


def test_agentcore_gateway_url_requires_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """region 無しに gateway へのリクエストは署名できない。

    欠けたまま進むと検索のたびに失敗するため、起動時に要求する。
    """
    monkeypatch.delenv("AWS_REGION", raising=False)
    with pytest.raises(ValidationError, match="AWS_REGION"):
        Settings(agentcore_gateway_url=_VALID_AGENTCORE_GATEWAY_URLS[0])


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


# Redis (ElastiCache) IAM 認証。RDS IAM 認証 (上の _IAM_RUNTIME_URL 節) と同じ理由で
# URL の password を拒否する。加えて token は user 単位で署名するため、URL に
# username が無い設定 (RDS 側は host 必須だが Redis 側は user 必須) も矛盾として弾く。
# cache 名は署名の host で URL (DNS endpoint) からは導出できないため明示必須。
# IAM 認証は TLS 前提のため、rediss:// (TLS) 以外の scheme も拒否する。

_REDIS_IAM_REGION = "ap-northeast-1"
_REDIS_IAM_CACHE_NAME = "vector-cache-abc123"
# happy path は TLS 必須の contract を満たす rediss:// を正本にする。
_IAM_REDIS_URL = "rediss://vector-app@vector-cache.abc.cache.amazonaws.com:6379/0"


def test_redis_iam_auth_defaults_to_false() -> None:
    """既定は無効。Fly / dev は URL の password で繋ぐ。"""
    assert Settings().redis_iam_auth is False


def test_redis_iam_cache_name_defaults_to_none() -> None:
    """IAM 認証を使わない環境では未設定が正しい。"""
    assert Settings().redis_iam_cache_name is None


def test_redis_iam_auth_accepts_passwordless_url_with_signing_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rediss:// (TLS) + username + region + cache 名が揃えば通る (happy path)。"""
    monkeypatch.setenv("REDIS_URL", _IAM_REDIS_URL)
    monkeypatch.setenv("AWS_REGION", _REDIS_IAM_REGION)
    monkeypatch.setenv("REDIS_IAM_CACHE_NAME", _REDIS_IAM_CACHE_NAME)
    s = Settings(redis_iam_auth=True)
    assert s.redis_iam_auth is True


def test_redis_iam_auth_rejects_non_tls_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    """auth token は平文で流せない。IAM 認証には rediss:// (TLS) を要求する。"""
    monkeypatch.setenv(
        "REDIS_URL", "redis://vector-app@vector-cache.abc.cache.amazonaws.com:6379/0"
    )
    monkeypatch.setenv("AWS_REGION", _REDIS_IAM_REGION)
    monkeypatch.setenv("REDIS_IAM_CACHE_NAME", _REDIS_IAM_CACHE_NAME)
    with pytest.raises(ValidationError, match="REDIS_IAM_AUTH"):
        Settings(redis_iam_auth=True)


def test_redis_iam_auth_rejects_password_in_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """password が残っていると「IAM のつもりで password 認証」を疑えなくなる。"""
    monkeypatch.setenv(
        "REDIS_URL",
        "redis://vector-app:leftover@vector-cache.abc.cache.amazonaws.com:6379/0",
    )
    monkeypatch.setenv("AWS_REGION", _REDIS_IAM_REGION)
    monkeypatch.setenv("REDIS_IAM_CACHE_NAME", _REDIS_IAM_CACHE_NAME)
    with pytest.raises(ValidationError, match="REDIS_IAM_AUTH"):
        Settings(redis_iam_auth=True)


def test_redis_iam_auth_rejects_url_without_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """token は user 単位で署名するため、誰として繋ぐか不明な設定は矛盾として弾く。"""
    monkeypatch.setenv(
        "REDIS_URL", "redis://vector-cache.abc.cache.amazonaws.com:6379/0"
    )
    monkeypatch.setenv("AWS_REGION", _REDIS_IAM_REGION)
    monkeypatch.setenv("REDIS_IAM_CACHE_NAME", _REDIS_IAM_CACHE_NAME)
    with pytest.raises(ValidationError, match="REDIS_IAM_AUTH"):
        Settings(redis_iam_auth=True)


def test_redis_iam_auth_requires_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """region 無しに token は署名できない。"""
    monkeypatch.setenv("REDIS_URL", _IAM_REDIS_URL)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setenv("REDIS_IAM_CACHE_NAME", _REDIS_IAM_CACHE_NAME)
    with pytest.raises(ValidationError, match="AWS_REGION"):
        Settings(redis_iam_auth=True)


def test_redis_iam_auth_requires_cache_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """cache 名は署名の host であり、endpoint の URL からは導出できない。"""
    monkeypatch.setenv("REDIS_URL", _IAM_REDIS_URL)
    monkeypatch.setenv("AWS_REGION", _REDIS_IAM_REGION)
    monkeypatch.delenv("REDIS_IAM_CACHE_NAME", raising=False)
    with pytest.raises(ValidationError, match="REDIS_IAM_CACHE_NAME"):
        Settings(redis_iam_auth=True)


def test_redis_iam_auth_disabled_allows_password_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """無効時は従来通り URL の password で繋げる (Fly / dev の既定)。"""
    password_url = "redis://:secret@localhost:6379/0"
    monkeypatch.setenv("REDIS_URL", password_url)
    s = Settings()
    assert s.redis_url == password_url
