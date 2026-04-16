"""gemini-embedding-001 を用いた Gemini Embedder 実装。"""

from __future__ import annotations

from google import genai
from google.genai import types
from google.genai.errors import APIError, ServerError

from app.analysis.embedder.base import BaseEmbedder
from app.analysis.errors import (
    AnalysisDomainError,
    ConfigurationError,
    InvalidInputError,
    NetworkError,
    ProviderError,
    RateLimitError,
    UnclassifiedError,
)
from app.config import settings


class GeminiEmbedder(BaseEmbedder):
    """BaseEmbedder の gemini-embedding-001 実装。"""

    MODEL = "gemini-embedding-001"
    DIMENSION = 768
    RPM = 15  # batchEmbedContents エンドポイント（SDK は常にこちらを使う）
    RPD = 1500

    def __init__(self) -> None:
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise ConfigurationError("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=api_key)

    async def _call_api(
        self, contents: str | list[str], task_type: str
    ) -> list[list[float]]:
        """Gemini の embed_content API を呼び出す。

        - contents が str のとき  -> embedContent  (500 RPM)
        - contents が list のとき -> batchEmbedContents (15 RPM)
        """
        response = await self._client.aio.models.embed_content(
            model=self.MODEL,
            contents=contents,
            config=types.EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=self.DIMENSION,
            ),
        )
        return [e.values for e in response.embeddings]

    def _translate_error(self, exc: Exception) -> AnalysisDomainError:
        """Gemini SDK の例外を原因の所在で分類する。"""
        if isinstance(exc, APIError):
            status = exc.status or ""
            message = exc.message or ""

            if "reported as leaked" in message:
                return ConfigurationError(f"API key leaked: {message}")

            if status in (
                "UNAUTHENTICATED",
                "PERMISSION_DENIED",
                "FAILED_PRECONDITION",
                "NOT_FOUND",
            ):
                return ConfigurationError(f"{status}: {message}")

            if status in ("INVALID_ARGUMENT", "DEADLINE_EXCEEDED"):
                return InvalidInputError(f"{status}: {message}")

            if status == "RESOURCE_EXHAUSTED":
                return RateLimitError(f"{status}: {message}")

            if isinstance(exc, ServerError):
                return ProviderError(f"{status}: {message}")

            return UnclassifiedError(
                f"Unhandled APIError {exc.code} {status}: {message}"
            )

        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return NetworkError(f"{type(exc).__name__}: {exc}")

        return UnclassifiedError(f"{type(exc).__name__}: {exc}")
