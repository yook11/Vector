"""Embedding error hierarchy."""


class EmbeddingError(Exception):
    """Raised when embedding generation fails (base / unclassifiable)."""


class RateLimitError(EmbeddingError):
    """HTTP 429 — rate limit exceeded."""


class TransientError(EmbeddingError):
    """5xx, network errors, timeouts — expected to recover with time."""


class InvalidInputError(EmbeddingError):
    """4xx (except 429) — input-caused, permanent. Retry is pointless."""


class DailyQuotaExhaustedError(EmbeddingError):
    """RPD limit reached — no more requests allowed today."""
