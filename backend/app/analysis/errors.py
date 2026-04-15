"""Analysis domain error hierarchy.

Classified by cause origin so that receivers can immediately determine
"what happened" and "whose problem it is".
"""


class AnalysisDomainError(Exception):
    """Base for all analysis domain errors (analyzer + embedder)."""


class InvalidInputError(AnalysisDomainError):
    """Input problem (bad prompt, too long) — skip this article."""


class ConfigurationError(AnalysisDomainError):
    """Configuration / authentication problem — stop all, notify operator."""


class ProviderError(AnalysisDomainError):
    """Provider-side problem (Google 5xx, broken response) — retry later."""


class NetworkError(AnalysisDomainError):
    """Communication problem (timeout, connection refused) — retry later."""


class RateLimitError(AnalysisDomainError):
    """Rate limit exceeded (HTTP 429 / RESOURCE_EXHAUSTED) — wait and retry."""


class DailyQuotaExhaustedError(AnalysisDomainError):
    """RPD limit reached — stop until tomorrow."""


class UnclassifiedError(AnalysisDomainError):
    """Unknown cause — log and investigate."""
