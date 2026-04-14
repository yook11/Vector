"""Analysis error hierarchy."""


class AnalysisError(Exception):
    """Raised when AI analysis fails (base / unclassifiable)."""


class RateLimitError(AnalysisError):
    """HTTP 429 — rate limit exceeded."""


class TransientError(AnalysisError):
    """5xx, network errors, timeouts — expected to recover with time."""


class InvalidInputError(AnalysisError):
    """4xx (except 429) — input-caused, permanent. Retry is pointless."""


class DailyQuotaExhaustedError(AnalysisError):
    """RPD limit reached — no more requests allowed today."""
