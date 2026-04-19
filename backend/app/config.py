from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py から 2 階層上がプロジェクトルート
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"


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
    content_max_length: int = 8000
    content_max_concurrent: int = 10  # 同時 HTTP 接続数の上限
    content_domain_delay: float = 1.0  # 同一ドメインへのリクエスト間隔（秒）
    content_max_fetch_attempts: int = 3  # N 回失敗した記事はスキップ

    # 内部 API（BFF プロキシ信頼）
    internal_api_secret: SecretStr = SecretStr("change-me-in-production")

    # アプリ URL
    frontend_url: str = "http://localhost:3000"
    backend_url: str = "http://localhost:8000"

    # Hacker News API
    hn_api_base_url: str = "https://hn.algolia.com/api/v1"
    hn_min_points: int = 20
    hn_hits_per_page: int = 50

    # Alpha Vantage API
    av_api_key: SecretStr = SecretStr("")
    av_api_base_url: str = "https://www.alphavantage.co/query"
    av_topics: str = "technology"
    av_limit: int = 50

    # セマンティック検索
    semantic_search_max_distance: float = 0.8  # コサイン距離のしきい値

    # タスクキュー
    redis_url: str = "redis://localhost:6379/0"


settings = Settings()
