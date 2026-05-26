"""gemini-embedding-001 を用いた Search BC (query) 専用 Embedder 実装。

``task_type="RETRIEVAL_QUERY"`` を固定する (RETRIEVAL_DOCUMENT 経路は Stage 5 の
``app/analysis/embedding/ai/gemini.py`` に独立、本 class は query に専念)。

``_translate_error`` は Stage 4 ``GeminiAssessor._translate_error`` / Stage 5
``GeminiEmbedder._translate_error`` と完全同形だが、解いている問題が違うため
独立 hierarchy として複製する (memory `feedback_no_share_different_problems`)。
"""

from __future__ import annotations

import httpx
import structlog
from google import genai
from google.genai import errors as genai_errors
from google.genai.types import EmbedContentConfig

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderQuotaExhaustedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
)
from app.config import settings
from app.search.embedding.base import QueryEmbedder

logger = structlog.get_logger(__name__)


class GeminiQueryEmbedder(QueryEmbedder):
    """QueryEmbedder の gemini-embedding-001 実装 (Search query 専用)。"""

    MODEL = "gemini-embedding-001"
    DIMENSION = 768
    QUERY_PREFIX = ""

    def __init__(self) -> None:
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            # Phase 4: 引数 message は SAFE_ATTRS 外。CODE と起動ログで識別。
            raise AIProviderConfigurationError()
        self._client = genai.Client(api_key=api_key)

    async def _call_api(self, text: str) -> list[float]:
        """Gemini ``embed_content`` API を ``RETRIEVAL_QUERY`` で呼び出す。

        provider response shape 違反 (embeddings 空 / values None) は
        ``AIProviderRequestInvalidError`` で raise する。
        """
        response = await self._client.aio.models.embed_content(
            model=self.MODEL,
            contents=text,
            config=EmbedContentConfig(
                output_dimensionality=self.DIMENSION,
                task_type="RETRIEVAL_QUERY",
            ),
        )
        embeddings = response.embeddings
        if not embeddings:
            # Phase 4: SDK response (PII 含有) を __str__ 経路に乗せない。
            raise AIProviderRequestInvalidError()
        first = embeddings[0]
        if first.values is None:
            raise AIProviderRequestInvalidError()
        return list(first.values)

    def _translate_error(self, exc: Exception) -> Exception:
        """Gemini SDK / httpx 例外を ``AIProvider*Error`` 階層に翻訳する。

        Stage 4 / Stage 5 と 1:1 同形 (memory `feedback_no_share_different_problems`
        に従い共用せず複製)。マップできなければ ``exc`` をそのまま return
        (caller である ``_embed_once`` が bare re-raise する規約)。
        """
        # network 系 (httpx は SDK の transport)
        # Phase 4: AIProvider*Error は VectorDomainError 継承で __str__ が SAFE_ATTRS
        # 経路のみ。str(exc) を引き渡しても __str__ には出ないが、call site を
        # 明示的に空引数化して PII 含有経路が残っていないことを grep で示す。
        if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
            return AIProviderNetworkError()
        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return AIProviderNetworkError()

        # genai SDK の例外階層 (HTTP status + gRPC status の両方を見る)
        if isinstance(exc, genai_errors.ClientError):
            code = getattr(exc, "code", None)
            status = getattr(exc, "status", None) or ""
            raw_message = str(getattr(exc, "message", "")) or str(exc)
            message = raw_message.lower()

            # red-team chain γ-1: SDK 生 message に key prefix /
            # Authorization header が含まれる経路があるため固定文言に丸める。
            if "reported as leaked" in message:
                return AIProviderConfigurationError()

            if code == 400 or status == "INVALID_ARGUMENT":
                if "api key" in message or "permission" in message:
                    return AIProviderConfigurationError()
                if "blocked" in message or "safety" in message:
                    return AIProviderInputRejectedError()
                return AIProviderRequestInvalidError()
            if code in (401, 403, 404) or status in (
                "UNAUTHENTICATED",
                "PERMISSION_DENIED",
                "NOT_FOUND",
                "FAILED_PRECONDITION",
            ):
                return AIProviderConfigurationError()
            if code == 429 or status == "RESOURCE_EXHAUSTED":
                if "quota" in message or "daily" in message:
                    return AIProviderQuotaExhaustedError()
                return AIProviderRateLimitedError()

        if isinstance(exc, genai_errors.ServerError):
            return AIProviderServiceUnavailableError()

        return exc  # bare re-raise (UNKNOWN)
