from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlparse

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.exc import ArgumentError

from app.db_ssl import parse_sslmode

# backend/app/config.py から 2 階層上がプロジェクトルート
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"

# BFF プロキシ認証で fail-open にしないため、起動時に拒否する既知の弱秘密。
# `.env.example` のプレースホルダや典型的な暫定値が production にそのまま
# 残るのを防ぐ（共有秘密の偽装による admin 権限取得対策）。
_KNOWN_WEAK_INTERNAL_SECRETS = frozenset(
    {
        "change-me-in-production",
        "change-me",
        "changeme",
        "secret",
        "password",
    }
)
_INTERNAL_API_SECRET_MIN_LENGTH = 32

# DATABASE_URL に含まれていれば起動時拒否する公開済 dev default / placeholder。
# application 接続の弱秘密だけを対象にし、migration role の dev/CI default は除外する。
_KNOWN_WEAK_DATABASE_URL_PATTERNS = frozenset(
    {
        "vector_app:vector_app",
        "<set-strong-password",
    }
)

# revalidate 通知 (internal_frontend_base_url) の宛先ホスト allowlist。
# notifier (FrontendRevalidateNotifier) は SSRF guard をバイパスして
# REVALIDATE_BEARER_SECRET を Bearer 送信するため、宛先が攻撃者制御に向くと
# secret 持ち出し経路になる。env 値が攻撃者ホストに向かないことを起動時に構造検証する。
# global allowlist は全環境共通、本番は *.flycast に絞る (production narrowing)。
_ALLOWED_INTERNAL_FRONTEND_HOSTS = frozenset({"localhost", "127.0.0.1", "frontend"})
_ALLOWED_INTERNAL_FRONTEND_HOST_SUFFIX = ".flycast"

# production で DB 接続文字列に要求する TLS sslmode。Neon は public internet
# 越しのため平文 (disable / allow / prefer / 未指定) を起動時に拒否する。
# sslmode の解釈と allowlist は db_ssl.parse_sslmode に SSoT 化 (二重定義回避)。
_PRODUCTION_REQUIRED_SSLMODES = frozenset({"require", "verify-ca", "verify-full"})


def _internal_frontend_host(url: str) -> str | None:
    """internal_frontend_base_url から host を取り出す (小文字化・port 除去済)。"""
    return urlparse(url).hostname


def _assert_strong_secret(raw: str, name: str) -> None:
    """BFF↔backend 共有秘密の強度を起動時に検証する。

    既知の弱秘密や短すぎる値を ValueError として弾き、`.env` の設定漏れが
    サイレントに fail-open するのを防ぐ。``name`` は error message 用の env 名。
    """
    if raw.lower() in _KNOWN_WEAK_INTERNAL_SECRETS:
        raise ValueError(
            f"{name} is set to a known weak default; "
            "generate a new one with `openssl rand -hex 32`"
        )
    if len(raw) < _INTERNAL_API_SECRET_MIN_LENGTH:
        raise ValueError(
            f"{name} must be at least {_INTERNAL_API_SECRET_MIN_LENGTH} "
            "characters; generate one with `openssl rand -hex 32`"
        )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")

    # デプロイ環境識別。production では FastAPI 自動 docs を無効化する。
    env: Literal["development", "production"] = "development"

    # データベース (application 接続)
    # application runtime は最小権限 role で接続する。env 必須化と弱秘密拒否で
    # production への dev fallback 混入を防ぐ。
    database_url: str

    # データベース (migration role)
    # alembic / pytest fixture / vector_test 作成など admin 系の作業では
    # ``vector`` (table owner) で接続する。``database_url`` と分離することで、
    # application 経路は最小権限 (vector_app) のままにできる。
    # 未設定時は ``database_url`` にフォールバックし、後方互換を保つ。
    migration_database_url: str | None = None

    # データベース (application role passwords)。権限境界テスト用に settings 経由で
    # 取得し、production runtime では password 単体としては読まない。
    postgres_auth_password: SecretStr | None = None
    postgres_app_password: SecretStr | None = None
    postgres_collect_password: SecretStr | None = None

    # AI
    # Stage 3 (extraction) と Stage 4 (assessment) のアダプター選択は env では
    # なく brokers.py の composition root (_wire_analysis_adapters) で hardcode する。
    # 切替はコード変更 + worker restart で行うため、ここに provider 名は持たない。
    gemini_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    deepseek_api_key: SecretStr = SecretStr("")

    # ニュース取得
    max_articles_per_fetch: int = 50
    max_analysis_per_run: int = 200

    # 分析
    max_analysis_consecutive_failures: int = 3  # サーキットブレーカー

    # 本文抽出
    content_max_concurrent: int = 10  # 同時 HTTP 接続数の上限
    content_domain_delay: float = 1.0  # 同一ドメインへのリクエスト間隔（秒）
    content_max_fetch_attempts: int = 3  # N 回失敗した記事はスキップ

    # 内部 API（BFF プロキシ信頼）— 2 つの trust 境界を別 secret で分離する。
    # - bff_jwt_signing_secret: BFF→backend の HS256 JWT 署名/検証鍵
    # - revalidate_bearer_secret: backend→frontend revalidate の Bearer
    # どちらも必須 (default なし)。強度検査 / 同一値拒否は
    # _validate_internal_secrets が担う。
    bff_jwt_signing_secret: SecretStr
    revalidate_bearer_secret: SecretStr

    # アプリ URL
    # ``frontend_url`` は CORS の allow_origins などブラウザ起源 URL に使う。
    # backend → frontend container を直接呼び出す経路 (例: revalidate 通知)
    # では compose 内部 DNS や同一 VPC 内ホスト名が必要なため
    # ``internal_frontend_base_url`` を別途用意する。
    # default 値を持たせず、env 入れ忘れは Pydantic の起動時検証で止める。
    frontend_url: str
    internal_frontend_base_url: str

    # タスクキュー
    redis_url: str = "redis://localhost:6379/0"

    # back-fill (パイプライン保守)
    # curation は救済機構の前提として常時有効。assessments / embeddings は
    # 明示的に有効化する。
    backfill_curations_enabled: bool = True
    backfill_assessments_enabled: bool = False
    backfill_embeddings_enabled: bool = False

    # pipeline_events retention。kill switch + batch 上限で purge 負荷を抑える。
    pipeline_events_retention_enabled: bool = True
    pipeline_events_retention_max_batches: int = 5

    # 可観測性 (Logfire)
    # token 未設定時は Logfire 送信を no-op にする。token は必ず settings 経由で
    # 観測層 bootstrap に渡す。
    logfire_token: SecretStr | None = None

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, v: str) -> str:
        """DB 接続文字列に公開済 default / placeholder が残らないことを起動時に強制。

        `.env` 設定漏れで弱秘密が production に滲むのを防ぐ。
        """
        for pattern in _KNOWN_WEAK_DATABASE_URL_PATTERNS:
            if pattern in v:
                raise ValueError(
                    "DATABASE_URL contains a known dev placeholder/weak password "
                    f"({pattern!r}); use a strong password generated with "
                    "`openssl rand -hex 32` and configure via .env"
                )
        return v

    @field_validator("internal_frontend_base_url")
    @classmethod
    def _validate_internal_frontend_base_url(cls, v: str) -> str:
        """revalidate 通知の宛先を既知の internal ホストに限定する (起動時 fail-fast)。

        notifier は SSRF guard をバイパスして REVALIDATE_BEARER_SECRET を Bearer
        送信するため、env 値が攻撃者制御のホストに向くと secret 持ち出し経路になる。
        全環境共通の global allowlist (localhost / 127.0.0.1 / frontend / *.flycast) で
        任意ホストへの送信を構造遮断する。本番のみの絞り込みは
        ``_enforce_flycast_in_production`` が担う。
        """
        scheme = urlparse(v).scheme
        if scheme not in ("http", "https"):
            raise ValueError(
                "INTERNAL_FRONTEND_BASE_URL must use http or https scheme, "
                f"got {scheme!r}"
            )
        host = _internal_frontend_host(v)
        if host is None:
            raise ValueError("INTERNAL_FRONTEND_BASE_URL must include a host")
        if host in _ALLOWED_INTERNAL_FRONTEND_HOSTS or host.endswith(
            _ALLOWED_INTERNAL_FRONTEND_HOST_SUFFIX
        ):
            return v
        raise ValueError(
            f"INTERNAL_FRONTEND_BASE_URL host {host!r} is not an allowed internal "
            "destination; expected localhost / 127.0.0.1 / frontend (compose) or a "
            "*.flycast host (Fly private network)"
        )

    @model_validator(mode="after")
    def _validate_internal_secrets(self) -> Self:
        """BFF↔backend trust 境界の 2 秘密を起動時に検証する。

        各 secret に強度検査をかけ、両者が同一値なら構造分離の意味を失うため拒否
        する。未設定は Pydantic の required field 検査が起動時に弾く。
        """
        _assert_strong_secret(
            self.bff_jwt_signing_secret.get_secret_value(), "BFF_JWT_SIGNING_SECRET"
        )
        _assert_strong_secret(
            self.revalidate_bearer_secret.get_secret_value(),
            "REVALIDATE_BEARER_SECRET",
        )

        # 同一値は構造分離を無効化するため拒否。
        if (
            self.bff_jwt_signing_secret.get_secret_value()
            == self.revalidate_bearer_secret.get_secret_value()
        ):
            raise ValueError(
                "BFF_JWT_SIGNING_SECRET and REVALIDATE_BEARER_SECRET must differ; "
                "using the same value defeats the secret split (a single leak "
                "would compromise both trust boundaries)"
            )

        return self

    @model_validator(mode="after")
    def _enforce_flycast_in_production(self) -> Self:
        """production では revalidate 宛先を *.flycast に限定する (narrowing)。

        dev host (localhost / 127.0.0.1 / frontend) は本番では到達できず silent fail に
        なるため、起動時に弾いて「本番は Fly private network の flycast」を構造的契約に
        する。dev / CI / test は env="development" のためこの絞り込みは効かない。
        host format 自体は ``_validate_internal_frontend_base_url`` が保証済で、
        ここは env 条件の narrowing のみ。
        """
        if self.env != "production":
            return self
        host = _internal_frontend_host(self.internal_frontend_base_url)
        if host is None or not host.endswith(_ALLOWED_INTERNAL_FRONTEND_HOST_SUFFIX):
            raise ValueError(
                "in production INTERNAL_FRONTEND_BASE_URL must be a *.flycast host "
                f"(Fly private network), got host {host!r}"
            )
        return self

    @model_validator(mode="after")
    def _require_ssl_in_production(self) -> Self:
        """production では DB 接続文字列に TLS sslmode を強制する (起動時 fail-fast)。

        Neon は public internet 越しの接続のため平文は不可。``database_url`` と
        (設定されていれば) ``migration_database_url`` の sslmode が
        require / verify-ca / verify-full のいずれかでなければ起動を拒否する。
        sslmode の解釈と allowlist は ``db_ssl.parse_sslmode`` に委譲し二重定義を
        避ける (typo は parse_sslmode が ValueError で弾く)。SSLContext の組み立て
        自体は接続側 (``db_ssl.create_app_engine``) に一元化し、config は本番の
        最低ラインだけを強制する。dev (docker 同一 network) は平文で良いため何も
        しない。
        """
        if self.env != "production":
            return self
        for name, url in (
            ("DATABASE_URL", self.database_url),
            ("MIGRATION_DATABASE_URL", self.migration_database_url),
        ):
            if url is None:
                continue
            try:
                sslmode = parse_sslmode(url)
            except ArgumentError as exc:
                # make_url が parse できない URL は ValueError に包んで Pydantic の
                # ValidationError 経路に乗せる (生の ArgumentError を漏らさない)。
                # password 漏洩を避けるため URL 自体は message に含めない。
                raise ValueError(f"{name} is not a parseable connection URL") from exc
            if sslmode not in _PRODUCTION_REQUIRED_SSLMODES:
                raise ValueError(
                    f"in production {name} must use a TLS sslmode "
                    f"({sorted(_PRODUCTION_REQUIRED_SSLMODES)}), got {sslmode!r}; "
                    "append `?sslmode=require` (connections to Neon cross the "
                    "public internet)"
                )
        return self


settings = Settings()
