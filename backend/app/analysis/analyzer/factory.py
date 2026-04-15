"""Analyzer factory and rate limiter construction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.analysis.analyzer.base import BaseAnalyzer
from app.config import settings

if TYPE_CHECKING:
    from app.infra.redis.rate_limiter import RateLimiter


def _build_limiters(
    analyzer_cls: type[BaseAnalyzer],
) -> dict[str, RateLimiter | None]:
    """Read ClassVars and build RateLimiter instances."""
    from app.infra.redis.cache import _get_client
    from app.infra.redis.rate_limiter import RateLimiter

    redis = _get_client()
    rpm_limiter: RateLimiter | None = None
    rpd_limiter: RateLimiter | None = None

    if analyzer_cls.RPM is not None:
        rpm_limiter = RateLimiter(
            redis=redis,
            key=f"ratelimit:{analyzer_cls.MODEL}:rpm",
            max_requests=analyzer_cls.RPM,
            window_seconds=60,
            block=True,
        )
    if analyzer_cls.RPD is not None:
        rpd_limiter = RateLimiter(
            redis=redis,
            key=f"ratelimit:{analyzer_cls.MODEL}:rpd",
            max_requests=analyzer_cls.RPD,
            window_seconds=86400,
            block=False,
        )
    return {"rpm_limiter": rpm_limiter, "rpd_limiter": rpd_limiter}


def get_analyzer() -> BaseAnalyzer:
    """Factory: return an analyzer instance based on settings.ai_provider.

    Reads ClassVars (RPM, RPD) from the analyzer class and builds
    RateLimiter instances to inject via the constructor.

    Raises:
        ValueError: If ai_provider is not supported.
    """
    provider = settings.ai_provider.lower()
    if provider == "gemini":
        from app.analysis.analyzer.gemini import GeminiAnalyzer

        limiters = _build_limiters(GeminiAnalyzer)
        return GeminiAnalyzer(**limiters)
    raise ValueError(f"Unsupported AI provider: {provider}")
