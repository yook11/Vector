"""Analysis domain error hierarchy.

Unified hierarchy for both AI analysis (analyzer) and embedding (embedder).
"""


class AnalysisDomainError(Exception):
    """Base for all analysis domain errors (analyzer + embedder)."""


class RateLimitError(AnalysisDomainError):
    """HTTP 429 — rate limit exceeded."""


class TransientError(AnalysisDomainError):
    """5xx, network errors, timeouts — expected to recover with time."""


class InvalidInputError(AnalysisDomainError):
    """4xx (except 429) — input-caused, permanent. Retry is pointless."""


class DailyQuotaExhaustedError(AnalysisDomainError):
    """RPD limit reached — no more requests allowed today."""
