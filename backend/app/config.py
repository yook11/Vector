from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://vector:vector@db:5432/vector"

    # AI
    ai_provider: str = "gemini"
    gemini_api_key: str = ""
    openai_api_key: str = ""

    # News Fetcher
    check_interval_minutes: int = 30
    max_articles_per_fetch: int = 50
    max_analysis_per_run: int = 10

    # Content extraction
    content_max_length: int = 8000
    content_max_concurrent: int = 10  # max simultaneous HTTP connections
    content_domain_delay: float = 1.0  # seconds between requests to same domain
    content_max_fetch_attempts: int = 3  # skip articles after N failed attempts

    # Embedding rate limit
    embed_batch_size: int = 10  # articles per API call
    embed_batch_interval: float = 8.0  # seconds between batches (~75 RPM)
    embed_rate_limit_delay: float = 60.0  # wait after 429
    embed_max_consecutive_failures: int = 3  # circuit breaker

    # Auth / JWT
    jwt_secret: str = "change-me-in-production-use-a-strong-random-secret"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60
    jwt_refresh_expire_days: int = 30
    jwt_refresh_grace_period_seconds: int = 10

    # App URLs
    frontend_url: str = "http://localhost:3000"
    backend_url: str = "http://localhost:8000"

    # Task Queue
    redis_url: str = "redis://localhost:6379/0"


settings = Settings()
