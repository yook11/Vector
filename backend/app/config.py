import re
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlparse

from pydantic import (
    EmailStr,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)
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

# DB 接続 URL に含まれていれば起動時拒否する公開済 dev default / placeholder。
# role 共通の placeholder を対象にし、migration role の dev/CI default は除外する。
_KNOWN_WEAK_DATABASE_URL_PATTERNS = frozenset(
    {
        "vector_app:vector_app",
        "vector_auth:vector_auth",
        "<set-strong-password",
    }
)
_DATABASE_URL_ENV_NAMES = {
    "database_url": "DATABASE_URL",
    "migration_database_url": "MIGRATION_DATABASE_URL",
    "auth_retention_database_url": "AUTH_RETENTION_DATABASE_URL",
}

# revalidate 通知 (internal_frontend_base_url) の宛先ホスト allowlist。
# notifier (FrontendRevalidateNotifier) は SSRF guard をバイパスして
# REVALIDATE_BEARER_SECRET を Bearer 送信するため、宛先が攻撃者制御に向くと
# secret 持ち出し経路になる。env 値が攻撃者ホストに向かないことを起動時に構造検証する。
# global allowlist は全環境共通、本番は内部 namespace に絞る (production narrowing)。
_ALLOWED_INTERNAL_FRONTEND_HOSTS = frozenset({"localhost", "127.0.0.1", "frontend"})
# 実行基盤が持つ内部 DNS namespace。先頭 dot が境界なので evilvector.internal は
# マッチしない。`.internal` は ICANN が private-use 用に予約した TLD で公開 DNS に
# 委任されないため、AWS 分を足しても外部ホストへの到達手段は増えない
# (`.flycast` は Fly が内部 resolver で名乗るだけで、予約の裏付けは無い)。
# 値は Terraform の `internal_namespace` と共有する契約 (infra/aws/variables.tf)。
# egress_proxy_url も同じ suffix で縛る。proxy は全 task の外向き通信の経路なので、
# 攻撃者ホストに向いた場合の射程は revalidate 宛先より広い。
_ALLOWED_INTERNAL_HOST_SUFFIXES = (".flycast", ".vector.internal")
# error message 用の表示形。suffix を足したときに message だけ古くなるのを防ぐ。
_INTERNAL_NAMESPACE_GLOBS = " / ".join(
    f"*{suffix}" for suffix in _ALLOWED_INTERNAL_HOST_SUFFIXES
)

# production で DB 接続文字列に要求する TLS sslmode。Neon は public internet
# 越しのため平文 (disable / allow / prefer / 未指定) を起動時に拒否する。
# sslmode の解釈と allowlist は db_ssl.parse_sslmode に SSoT 化 (二重定義回避)。
_PRODUCTION_REQUIRED_SSLMODES = frozenset({"require", "verify-ca", "verify-full"})

# Logfire write token の形式 (pylf_v1_<region 2文字>_<英数字>)。region は us / eu に
# 限らず将来増えうるため固定列挙せず 2 文字の構造でのみ縛り、別 token の取り違えと
# 端末 paste 由来の制御文字 / 空白混入を起動時に弾く。
# `\A...\Z` で末尾改行直前にマッチする `$` の罠を避け、文字列全体を厳格に縛る。
_LOGFIRE_TOKEN_PATTERN = re.compile(r"\Apylf_v1_[a-z]{2}_[A-Za-z0-9]+\Z")


def _internal_host(url: str) -> str | None:
    """内部宛先 URL から host を取り出す (小文字化・port 除去済)。"""
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
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE), extra="ignore", hide_input_in_errors=True
    )

    # デプロイ環境識別。production では FastAPI 自動 docs を無効化する。
    env: Literal["development", "production"] = "development"

    # データベース (application 接続)
    # application runtime は最小権限 role で接続する。env 必須化と弱秘密拒否で
    # production への dev fallback 混入を防ぐ。
    database_url: str

    # RDS IAM 認証。有効時は URL に password を持たせず、接続ごとに IAM の auth token
    # を作って認証する (``app/db_iam_auth.py``)。**射程は app runtime の接続だけ**で、
    # migration は migrator role の password 認証を続ける。
    db_iam_auth: bool = False

    # token 署名に使う AWS region。botocore が region に読む env は AWS_DEFAULT_REGION
    # だけで、ECS が注入する AWS_REGION は見ない。解決規則に任せると本番の全 task が
    # engine 生成で NoRegionError になるため、ここで受けて明示的に渡す。
    aws_region: str | None = None

    # データベース (migration role)
    # alembic / pytest fixture / vector_test 作成など admin 系の作業では
    # ``vector`` (table owner) で接続する。``database_url`` と分離することで、
    # application 経路は最小権限 (vector_app) のままにできる。
    # 未設定時は ``database_url`` にフォールバックし、後方互換を保つ。
    migration_database_url: str | None = None

    # データベース (auth schema maintenance)
    # Better Auth が管理する auth."rateLimit" retention など、auth schema 内の
    # 保守処理だけが使う。通常 application role (vector_app) には auth.* DML を
    # 広げないため、vector_auth 相当の接続文字列を別設定にする。
    auth_retention_database_url: str | None = None

    # データベース (application role passwords)。権限境界テスト用に settings 経由で
    # 取得し、production runtime では password 単体としては読まない。
    postgres_auth_password: SecretStr | None = None
    postgres_app_password: SecretStr | None = None
    postgres_collect_password: SecretStr | None = None

    # AI
    # Stage 3 (curation) と Stage 4 (assessment) のアダプター選択は env では
    # なく brokers.py の composition root (_wire_analysis_adapters) で hardcode する。
    # 切替はコード変更 + worker restart で行うため、ここに provider 名は持たない。
    gemini_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    deepseek_api_key: SecretStr = SecretStr("")
    tavily_api_key: SecretStr = SecretStr("")

    # 外部検索の入口。AgentCore Gateway の MCP endpoint で、gateway ID は apply 時に
    # 確定するため URL からしか知れない。値を持つのは agent 段 (実呼び出し) と
    # api 段 (research 開始 API の設定プリフライト) だけ。
    agentcore_gateway_url: str | None = None

    # ニュース取得
    max_articles_per_fetch: int = 50
    max_analysis_per_run: int = 200
    # CI/test は予約ドメインの dummy、本番は受信可能な専用 alias を設定する。
    crossref_contact_email: EmailStr | Literal["crossref-contact@example.invalid"]

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

    # 外向き通信の経路。設定されていれば ``make_external_async_client`` が全 client に
    # proxy として差し込む。未設定なら直接接続 (Fly / compose の既定)。
    # httpx は transport を明示すると env の proxy を読まないため、HTTPS_PROXY だけでは
    # この経路に効かない。env を読む SDK 経路、この settings 経路、proxy を経由しない
    # 内部宛経路の 3 通りに分かれる。
    egress_proxy_url: str | None = None

    # タスクキュー
    redis_url: str = "redis://localhost:6379/0"

    # ElastiCache IAM 認証。有効時は URL に password を持たせず、接続ごとに SigV4 の
    # auth token を作って認証する (``app/redis/iam_auth.py``)。どの user で繋ぐかは
    # ``redis_url`` の username が単一の情報源。
    redis_iam_auth: bool = False

    # token 署名の host に使う cache 名 (replication group id)。署名対象は DNS
    # endpoint ではなく cache 名で、URL からは導出できないため明示的に受ける。
    redis_iam_cache_name: str | None = None

    # back-fill (パイプライン保守)
    # curation は救済機構の前提として常時有効。assessments / embeddings は
    # 明示的に有効化する。
    backfill_curations_enabled: bool = True
    backfill_assessments_enabled: bool = False
    backfill_embeddings_enabled: bool = False

    # pipeline_events retention。kill switch + batch 上限で purge 負荷を抑える。
    pipeline_events_retention_enabled: bool = True
    pipeline_events_retention_max_batches: int = 5

    # Better Auth rateLimit retention。auth."rateLimit" は一時 counter であり、
    # enforcement window 経過後の長期保持を避ける。
    auth_rate_limit_retention_enabled: bool = True
    auth_rate_limit_retention_max_batches: int = 5

    # 可観測性 (Logfire)
    # token 未設定時は Logfire 送信を no-op にする。token は必ず settings 経由で
    # 観測層 bootstrap に渡す。
    logfire_token: SecretStr | None = None

    @field_validator(
        "database_url", "migration_database_url", "auth_retention_database_url"
    )
    @classmethod
    def _validate_database_url(cls, v: str | None, info: ValidationInfo) -> str | None:
        """DB 接続文字列に公開済 default / placeholder が残らないことを起動時に強制。

        `.env` 設定漏れで弱秘密が production に滲むのを防ぐ。
        """
        if v is None:
            return v
        # 型チェッカは property の narrowing をしないため、ローカルに束縛して判定する。
        field_name = info.field_name
        if field_name is None:
            raise ValueError("internal error: missing field name in validator info")
        env_name = _DATABASE_URL_ENV_NAMES[field_name]
        for pattern in _KNOWN_WEAK_DATABASE_URL_PATTERNS:
            if pattern in v:
                raise ValueError(
                    f"{env_name} contains a known dev placeholder/weak password "
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
        全環境共通の global allowlist (localhost / 127.0.0.1 / frontend / 実行基盤の
        内部 namespace) で任意ホストへの送信を構造遮断する。本番のみの絞り込みは
        ``_enforce_internal_namespace_in_production`` が担う。
        """
        scheme = urlparse(v).scheme
        if scheme not in ("http", "https"):
            raise ValueError(
                "INTERNAL_FRONTEND_BASE_URL must use http or https scheme, "
                f"got {scheme!r}"
            )
        host = _internal_host(v)
        if host is None:
            raise ValueError("INTERNAL_FRONTEND_BASE_URL must include a host")
        if host in _ALLOWED_INTERNAL_FRONTEND_HOSTS or host.endswith(
            _ALLOWED_INTERNAL_HOST_SUFFIXES
        ):
            return v
        raise ValueError(
            f"INTERNAL_FRONTEND_BASE_URL host {host!r} is not an allowed internal "
            "destination; expected localhost / 127.0.0.1 / frontend (compose) or an "
            f"internal namespace host ({_INTERNAL_NAMESPACE_GLOBS})"
        )

    @field_validator("egress_proxy_url")
    @classmethod
    def _validate_egress_proxy_url(cls, v: str | None) -> str | None:
        """外向き通信の経路を内部 namespace のホストに限定する (起動時 fail-fast)。

        この値は全 task の全外向き通信が通る経路なので、攻撃者ホストに向いた場合の
        射程は revalidate 宛先より広い (平文 http の上流では header ごと渡る)。
        dev host は許さない: proxy が居るのは AWS だけで、他環境では未設定が正しい。
        """
        if v is None:
            return v
        scheme = urlparse(v).scheme
        if scheme not in ("http", "https"):
            raise ValueError(
                f"EGRESS_PROXY_URL must use http or https scheme, got {scheme!r}"
            )
        host = _internal_host(v)
        if host is None:
            raise ValueError("EGRESS_PROXY_URL must include a host")
        if not host.endswith(_ALLOWED_INTERNAL_HOST_SUFFIXES):
            raise ValueError(
                f"EGRESS_PROXY_URL host {host!r} is not an internal namespace host "
                f"({_INTERNAL_NAMESPACE_GLOBS})"
            )
        return v

    @field_validator("agentcore_gateway_url")
    @classmethod
    def _validate_agentcore_gateway_url(cls, v: str | None) -> str | None:
        """外部検索の宛先を AWS 所有のホストに限定する (起動時 fail-fast)。

        内部宛 client (``make_internal_async_client``) は SSRF 検証を通さないため、
        宛先が正しいことの根拠はここにしかない。ただし
        ``_ALLOWED_INTERNAL_HOST_SUFFIXES`` は使えない: gateway の host は
        ``*.gateway.bedrock-agentcore.<region>.amazonaws.com`` で内部 namespace には
        属さない。``gateway.bedrock-agentcore`` まで literal で縛らないのは、
        AWS の命名規則が変わったときに静かに壊れないようにするため
        (infra 側も同じ理由で suffix を推測せず gateway_url から host を取っている)。
        """
        if v is None:
            return v
        scheme = urlparse(v).scheme
        if scheme != "https":
            raise ValueError(
                f"AGENTCORE_GATEWAY_URL must use https scheme, got {scheme!r}"
            )
        host = _internal_host(v)
        if host is None:
            raise ValueError("AGENTCORE_GATEWAY_URL must include a host")
        if not host.endswith(".amazonaws.com"):
            raise ValueError(
                f"AGENTCORE_GATEWAY_URL host {host!r} is not an AWS-owned host "
                "(*.amazonaws.com)"
            )
        return v

    @field_validator("logfire_token")
    @classmethod
    def _validate_logfire_token(cls, v: SecretStr | None) -> SecretStr | None:
        """Logfire write token の形式を起動時に検証する (fail-fast)。

        token 未設定 (dev / CI / test) は no-op 送信のため許容する。設定済みなら、
        端末 paste 由来の制御文字 / 空白混入や別 token の取り違えを起動時に弾く。
        Logfire ingest は壊れた token を 400 / 401 で黙って蹴り続け observability が
        サイレントに 0 になる (実際に ESC バイト混入で全 export が 400 になった) ため、
        誤設定を起動時に可視化する。error message に token 値は載せない。
        """
        if v is None:
            return v
        raw = v.get_secret_value()
        if raw != raw.strip() or any(ord(c) < 32 or ord(c) == 127 for c in raw):
            raise ValueError(
                "LOGFIRE_TOKEN contains whitespace or control characters; re-set the "
                "secret as plain text without stray bytes (a terminal paste can inject "
                "an ESC/newline byte)"
            )
        if not _LOGFIRE_TOKEN_PATTERN.match(raw):
            raise ValueError(
                "LOGFIRE_TOKEN does not match the expected Logfire write-token format "
                "'pylf_v1_<region>_<token>'; copy a write token from the Logfire "
                "dashboard (Settings -> Write tokens)"
            )
        return v

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
    def _require_crossref_contact_in_production(self) -> Self:
        """production では Crossref から連絡可能な alias を必須にする。"""
        if (
            self.env == "production"
            and self.crossref_contact_email == "crossref-contact@example.invalid"
        ):
            raise ValueError(
                "in production CROSSREF_CONTACT_EMAIL must be a monitored alias; "
                "the example.invalid value is reserved for CI/test"
            )
        return self

    @model_validator(mode="after")
    def _enforce_internal_namespace_in_production(self) -> Self:
        """production では revalidate 宛先を実行基盤の内部 namespace に限定する。

        dev host (localhost / 127.0.0.1 / frontend) は本番では到達できず silent fail に
        なるため、起動時に弾いて「本番の宛先は内部 namespace」を構造的契約にする。
        dev / CI / test は env="development" のためこの絞り込みは効かない。
        host format 自体は ``_validate_internal_frontend_base_url`` が保証済で、
        ここは env 条件の narrowing のみ。
        """
        if self.env != "production":
            return self
        host = _internal_host(self.internal_frontend_base_url)
        if host is None or not host.endswith(_ALLOWED_INTERNAL_HOST_SUFFIXES):
            raise ValueError(
                "in production INTERNAL_FRONTEND_BASE_URL must be an internal "
                f"namespace host ({_INTERNAL_NAMESPACE_GLOBS}), got host {host!r}"
            )
        return self

    @model_validator(mode="after")
    def _reject_password_when_iam_auth(self) -> Self:
        """IAM 認証時に runtime URL の password を拒否する (起動時 fail-fast)。

        provider が ``connect_args`` で password を上書きするため、URL 側の password は
        黙って無視される。「IAM のつもりで password 認証している」と疑えない状態を
        作らないために弾く。``migration_database_url`` は射程外
        (migrator role は password 認証を続ける)。
        """
        if not self.db_iam_auth:
            return self
        for field_name in ("database_url", "auth_retention_database_url"):
            raw: str | None = getattr(self, field_name)
            if raw is None:
                continue
            if urlparse(raw).password is not None:
                env_name = _DATABASE_URL_ENV_NAMES[field_name]
                raise ValueError(
                    f"DB_IAM_AUTH is enabled but {env_name} contains a password; "
                    "remove it (the IAM auth token replaces it)"
                )
        return self

    @model_validator(mode="after")
    def _require_region_when_iam_auth(self) -> Self:
        """IAM 認証には region が要る (起動時 fail-fast)。

        token は region ごとに署名するため、region が解決できないと botocore が
        ``NoRegionError`` を投げて engine が作れない。api では ``app/db.py`` の import
        時点で落ちるので、起動時に理由の読める形で弾く。
        """
        if self.db_iam_auth and self.aws_region is None:
            raise ValueError(
                "DB_IAM_AUTH is enabled but AWS_REGION is not set; "
                "the IAM auth token is signed per region"
            )
        return self

    @model_validator(mode="after")
    def _reject_password_when_redis_iam_auth(self) -> Self:
        """Redis IAM 認証時に URL の password を拒否し、user を要求する。

        DB 側 (``_reject_password_when_iam_auth``) と同じ理由で、「IAM のつもりで
        password 認証している」状態を起動時に弾く。加えて token は user ごとに
        署名するため、URL に username が無い設定も矛盾として弾く。
        """
        if not self.redis_iam_auth:
            return self
        parsed = urlparse(self.redis_url)
        if parsed.password is not None:
            raise ValueError(
                "REDIS_IAM_AUTH is enabled but REDIS_URL contains a password; "
                "remove it (the IAM auth token replaces it)"
            )
        if parsed.username is None:
            raise ValueError(
                "REDIS_IAM_AUTH is enabled but REDIS_URL has no user; "
                "the IAM auth token is signed per cache user"
            )
        return self

    @model_validator(mode="after")
    def _require_signing_inputs_when_redis_iam_auth(self) -> Self:
        """Redis IAM 認証には region と cache 名が要る (起動時 fail-fast)。

        region の事情は DB 側と同じ。cache 名は署名の host であり、endpoint の
        URL からは導出できないため、欠けたまま接続まで進ませない。
        """
        if not self.redis_iam_auth:
            return self
        if self.aws_region is None:
            raise ValueError(
                "REDIS_IAM_AUTH is enabled but AWS_REGION is not set; "
                "the IAM auth token is signed per region"
            )
        if self.redis_iam_cache_name is None:
            raise ValueError(
                "REDIS_IAM_AUTH is enabled but REDIS_IAM_CACHE_NAME is not set; "
                "the token is signed against the cache name, not the endpoint"
            )
        return self

    @model_validator(mode="after")
    def _require_region_when_agentcore_gateway(self) -> Self:
        """外部検索の SigV4 署名には region が要る (起動時 fail-fast)。

        署名は region ごとに作るため、欠けたまま進むと検索のたびに失敗する。
        """
        if self.agentcore_gateway_url is None:
            return self
        if self.aws_region is None:
            raise ValueError(
                "AGENTCORE_GATEWAY_URL is set but AWS_REGION is not; "
                "the gateway request is signed per region"
            )
        return self

    @model_validator(mode="after")
    def _require_tls_scheme_when_redis_iam_auth(self) -> Self:
        """Redis IAM 認証には rediss:// (TLS) が要る (起動時 fail-fast)。

        ElastiCache の IAM 認証は転送時暗号化が前提で、平文 scheme のままだと
        接続時の不透明なプロトコルエラーまで発現が遅れる。平文で auth token を
        流す構成もここで構造的に排除する。
        """
        if not self.redis_iam_auth:
            return self
        if urlparse(self.redis_url).scheme != "rediss":
            raise ValueError(
                "REDIS_IAM_AUTH is enabled but REDIS_URL is not rediss://; "
                "IAM auth requires TLS (the auth token must not travel in cleartext)"
            )
        return self

    @model_validator(mode="after")
    def _require_ssl_in_production(self) -> Self:
        """production では DB 接続文字列に TLS sslmode を強制する (起動時 fail-fast)。

        Neon は public internet 越しの接続のため平文は不可。``database_url`` と
        (設定されていれば) ``migration_database_url`` /
        ``auth_retention_database_url`` の sslmode が
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
            ("AUTH_RETENTION_DATABASE_URL", self.auth_retention_database_url),
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


# required field は env / .env から埋まるため、静的には「引数不足」に見える。
# この ignore は引数不足だけを黙らせる (他の型エラーは通す)。
settings = Settings()  # type: ignore[call-arg]
