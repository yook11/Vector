from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> project root is two levels up
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://vector:vector@db:5432/vector"

    # AI
    ai_provider: str = "gemini"
    ai_model_name: str = "gemini-2.5-flash-lite"
    gemini_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")

    # News Fetcher
    check_interval_minutes: int = 30
    max_articles_per_fetch: int = 50
    max_analysis_per_run: int = 200

    # Analysis rate limit
    analysis_request_interval: float = 4.0  # seconds between API requests (~15 RPM)

    # Content extraction
    content_max_length: int = 8000
    content_max_concurrent: int = 10  # max simultaneous HTTP connections
    content_domain_delay: float = 1.0  # seconds between requests to same domain
    content_max_fetch_attempts: int = 3  # skip articles after N failed attempts

    # Embedding
    embed_batch_size: int = 10  # articles per API call
    embed_max_consecutive_failures: int = 3  # circuit breaker

    # Internal API (BFF proxy trust)
    internal_api_secret: SecretStr = SecretStr("change-me-in-production")

    # App URLs
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
    av_max_daily_requests: int = 25

    # Duplicate detection
    dedup_similarity_threshold: float = 0.15  # cosine distance; lower = stricter
    dedup_time_window_days: int = 3  # compare articles within N days

    # Semantic search
    semantic_search_max_distance: float = 0.8  # cosine distance threshold

    # Task Queue
    redis_url: str = "redis://localhost:6379/0"


settings = Settings()
