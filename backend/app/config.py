from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlparse

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
# default 値を撤去しても、`.env` に dev fallback を貼り付けたまま production に
# 行く事故を防ぐ (red-team S-SECRET-1 防御)。public git history に焼き付いた
# application role の password ペアと .env.example の placeholder を blocklist 化。
# NOTE: ``vector:vector`` (migration role の dev/CI default) はここに含めない。
# CI postgres service の dummy として広く使われており、application 経路の
# ``database_url`` 検査としてはノイズ過多。本命は application role
# (``vector_app``) の password 漏洩経路。
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

    # デプロイ環境識別
    # production では FastAPI 自動 docs (/docs, /redoc, /openapi.json) を
    # 無効化する (red-team S-EXFIL-1 / C3 amplifier 防御)。CI / dev / test は
    # default の "development" で動き、production deploy 時のみ fly.toml [env]
    # で "production" を渡す。Literal で "prod" などの typo を起動時に reject。
    env: Literal["development", "production"] = "development"

    # データベース (application 接続)
    # red-team AUTH-N4: application runtime (FastAPI / worker / CLI) は
    # ``vector_app`` で接続し、public.* に schema-scoped アクセスする。
    # default 値を撤去し、env 必須化 + 公開済 dev password の起動時拒否で
    # production に dev fallback が滲む経路を構造的に塞ぐ (red-team S-SECRET-1)。
    database_url: str

    # データベース (migration role)
    # alembic / pytest fixture / vector_test 作成など admin 系の作業では
    # ``vector`` (table owner) で接続する。``database_url`` と分離することで、
    # application 経路は最小権限 (vector_app) のままにできる。
    # 未設定時は ``database_url`` にフォールバックし、後方互換を保つ。
    migration_database_url: str | None = None

    # データベース (application role passwords)
    # 権限境界の振る舞い検証 (tests/test_db_user_isolation.py) で別 user 接続を
    # 開くために settings 経由で取得する。CLAUDE.md の「os.environ 直参照禁止」
    # に従う。production runtime では DATABASE_URL / AUTH_DATABASE_URL に既に
    # 埋め込まれているため、ここでは password 単体としては読み出さない。
    postgres_auth_password: SecretStr | None = None
    postgres_app_password: SecretStr | None = None

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

    # 内部 API（BFF プロキシ信頼）— 2 つの trust 境界を別 secret で分離 (red-team
    # C1 防御)。1 secret 漏洩で両境界が陥落するのを防ぐ構造分離。
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
    # default 値は撤去 (red-team S-AUTH-4): production で env 入れ忘れた場合に
    # CORS が localhost:3000 で固まる / revalidate が compose 内部 DNS を叩いて
    # 失敗する fail-open を構造的に防ぐ。env 必須化により Pydantic が起動時に
    # ValidationError を投げる。
    frontend_url: str
    internal_frontend_base_url: str

    # タスクキュー
    redis_url: str = "redis://localhost:6379/0"

    # back-fill (パイプライン保守)
    # curation は救済機構 (年齢削除 / terminal_keep hold 明けの再投入) の前提と
    # して常時有効。assessments / embeddings は段階的有効化 (PLAN §8-6) のため
    # 既定 false のまま。
    backfill_curations_enabled: bool = True
    backfill_assessments_enabled: bool = False
    backfill_embeddings_enabled: bool = False

    # pipeline_events retention (red-team chain γ-4)
    # 90 日経過した監査行を毎時 :25 に purge する。kill switch + batch 上限で
    # 過負荷を抑える。MAX_BATCHES は source 増加時の逃げ道として動的拡張可能。
    pipeline_events_retention_enabled: bool = True
    pipeline_events_retention_max_batches: int = 5

    # 可観測性 (Logfire)
    # token は production のみ Fly secret (LOGFIRE_TOKEN) で投入する。未設定の
    # dev/CI/test では send_to_logfire="if-token-present" により logfire は完全
    # no-op (外部送信ゼロ)。``os.environ`` 直参照禁止の規約 (CLAUDE.md) に従い、
    # token は必ず settings 経由で観測層 bootstrap (observability.logfire_setup)
    # に渡す。
    logfire_token: SecretStr | None = None

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, v: str) -> str:
        """DB 接続文字列に公開済 default / placeholder が残らないことを起動時に強制。

        `.env` 設定漏れで `vector_app:vector_app` 等の弱秘密が production に滲むのを防ぐ
        (red-team S-SECRET-1 防御)。``_assert_strong_secret`` と同型の構造防御。
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


settings = Settings()
