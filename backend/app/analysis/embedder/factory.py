"""Embedder factory and rate limiter construction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.analysis.embedder.base import BaseEmbedder
from app.config import settings

if TYPE_CHECKING:
    from app.infra.redis.rate_limiter import RateLimiter


def _build_limiters(
    embedder_cls: type[BaseEmbedder],
) -> dict[str, RateLimiter | None]:
    """Read ClassVars and build RateLimiter instances."""
    from app.infra.redis.cache import _get_client
    from app.infra.redis.rate_limiter import RateLimiter

    redis = _get_client()
    rpm_limiter: RateLimiter | None = None
    rpd_limiter: RateLimiter | None = None

    if embedder_cls.RPM is not None:
        rpm_limiter = RateLimiter(
            redis=redis,
            key=f"ratelimit:{embedder_cls.MODEL}:rpm",
            max_requests=embedder_cls.RPM,
            window_seconds=60,
            block=True,
        )
    if embedder_cls.RPD is not None:
        rpd_limiter = RateLimiter(
            redis=redis,
            key=f"ratelimit:{embedder_cls.MODEL}:rpd",
            max_requests=embedder_cls.RPD,
            window_seconds=86400,
            block=False,
        )
    return {"rpm_limiter": rpm_limiter, "rpd_limiter": rpd_limiter}


def get_embedder() -> BaseEmbedder:
    """Factory: return an embedder instance based on settings.ai_provider.

    Reads ClassVars (RPM, RPD) from the embedder class and builds
    RateLimiter instances to inject via the constructor.

    Raises:
        ValueError: If ai_provider is not supported.
    """
    provider = settings.ai_provider.lower()
    if provider == "gemini":
        from app.analysis.embedder.gemini import GeminiEmbedder

        limiters = _build_limiters(GeminiEmbedder)
        return GeminiEmbedder(**limiters)
    raise ValueError(f"Unsupported AI provider for embeddings: {provider}")
