from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py から 2 階層上がプロジェクトルート
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"

# BFF プロキシ認証で fail-open にしないため、起動時に拒否する既知の弱秘密。
# `.env.example` のプレースホルダや典型的な暫定値が production にそのまま
# 残るのを防ぐ（INTERNAL_API_SECRET 偽装による admin 権限取得対策）。
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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")

    # データベース
    database_url: str = "postgresql+asyncpg://vector:vector@db:5432/vector"

    # AI
    ai_provider: str = "gemini"
    gemini_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")

    # Embedding (TEI ローカルサーバー)
    embedding_base_url: str = "http://embedding:80"

    # ニュース取得
    check_interval_minutes: int = 30
    max_articles_per_fetch: int = 50
    max_analysis_per_run: int = 200

    # 分析
    max_analysis_consecutive_failures: int = 3  # サーキットブレーカー

    # 本文抽出
    content_max_concurrent: int = 10  # 同時 HTTP 接続数の上限
    content_domain_delay: float = 1.0  # 同一ドメインへのリクエスト間隔（秒）
    content_max_fetch_attempts: int = 3  # N 回失敗した記事はスキップ

    # 内部 API（BFF プロキシ信頼）
    # デフォルト値を持たせない: .env で必ず強い乱数を設定させる（生成例:
    # `openssl rand -hex 32`）。未設定や弱秘密の場合は起動時に
    # ValidationError で落とす（_validate_internal_api_secret 参照）。
    internal_api_secret: SecretStr

    # アプリ URL
    frontend_url: str = "http://localhost:3000"
    backend_url: str = "http://localhost:8000"

    # Hacker News API
    hn_api_base_url: str = "https://hn.algolia.com/api/v1"
    hn_min_points: int = 20
    hn_hits_per_page: int = 50

    # セマンティック検索
    semantic_search_max_distance: float = 0.8  # コサイン距離のしきい値

    # タスクキュー
    redis_url: str = "redis://localhost:6379/0"

    # back-fill (パイプライン保守)
    # 既定 false。段階的有効化は PLAN §8-6 (Step 1 → 2 → 3) を参照。
    backfill_extractions_enabled: bool = False
    backfill_classifications_enabled: bool = False
    backfill_embeddings_enabled: bool = False

    @field_validator("internal_api_secret")
    @classmethod
    def _validate_internal_api_secret(cls, v: SecretStr) -> SecretStr:
        """BFF とバックエンド間の共有秘密に対する起動時バリデーション。

        既知の弱秘密や短すぎる値を ValidationError として弾き、
        `.env` の設定漏れがサイレントに fail-open するのを防ぐ。
        """
        raw = v.get_secret_value()
        if raw.lower() in _KNOWN_WEAK_INTERNAL_SECRETS:
            raise ValueError(
                "INTERNAL_API_SECRET is set to a known weak default; "
                "generate a new one with `openssl rand -hex 32`"
            )
        if len(raw) < _INTERNAL_API_SECRET_MIN_LENGTH:
            raise ValueError(
                "INTERNAL_API_SECRET must be at least "
                f"{_INTERNAL_API_SECRET_MIN_LENGTH} characters; "
                "generate one with `openssl rand -hex 32`"
            )
        return v


settings = Settings()
