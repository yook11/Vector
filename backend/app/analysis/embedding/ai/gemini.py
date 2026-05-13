"""gemini-embedding-001 を用いた Stage 5 (document 永続化) 専用 Embedder 実装。

google-genai SDK の ``embed_content`` API を非同期で呼び出してベクトルを取得する。
``output_dimensionality=768`` を固定で指定し、DB 側 ``HALFVEC(768)`` 列に適合する。
``task_type="RETRIEVAL_DOCUMENT"`` を固定する (RETRIEVAL_QUERY 経路は Search BC の
``app/search/embedding/gemini.py`` に独立、本 class は document に専念)。

SDK 例外翻訳は Stage 4 ``GeminiAssessor._translate_error`` と完全同形:
``AIProvider*Error`` 階層 (Layer 2-A、Stage 中立) に翻訳して raise する。Stage 5
marker (``Embedding*Error``) への詰め替えは Service 層 ACL の責務であり、本 class
では行わない。``_translate_error`` は未分類例外を ``exc`` として ``return`` し、
caller の bare re-raise guard 規約 (``BaseEmbedder._embed_once``) に委譲する。
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
from app.analysis.embedding.ai.base import BaseEmbedder
from app.config import settings

logger = structlog.get_logger(__name__)


class GeminiEmbedder(BaseEmbedder):
    """BaseEmbedder の gemini-embedding-001 実装 (Stage 5 document 専用)。"""

    PROVIDER = "gemini"
    MODEL = "gemini-embedding-001"
    DIMENSION = 768
    # Gemini API のレート制限値は tier に依存する。確定値が取れないため None で運用し、
    # 429 を structlog でモニタする (``AIProviderRateLimitedError`` として捕捉される)。
    RPM = None
    RPD = None
    DOCUMENT_PREFIX = ""

    def __init__(self) -> None:
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise AIProviderConfigurationError("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=api_key)

    async def _call_api(self, text: str) -> list[float]:
        """Gemini ``embed_content`` API を ``RETRIEVAL_DOCUMENT`` で呼び出す。

        ``embed_content`` レスポンスに ``finish_reason`` は存在しない
        (``EmbedContentResponse`` は ``embeddings`` / ``metadata`` /
        ``sdk_http_response`` が主)。safety / block 系は ``ClientError`` 経路の
        ``INVALID_ARGUMENT`` + ``"blocked"|"safety"`` message pattern で
        ``AIProviderInputRejectedError`` に寄せる (Stage 4 と同 pattern)。
        provider response shape 違反 (embeddings 空 / values None) は
        ``AIProviderRequestInvalidError`` で raise する。
        """
        response = await self._client.aio.models.embed_content(
            model=self.MODEL,
            contents=text,
            config=EmbedContentConfig(
                output_dimensionality=self.DIMENSION,
                task_type="RETRIEVAL_DOCUMENT",
            ),
        )
        embeddings = response.embeddings
        if not embeddings:
            raise AIProviderRequestInvalidError(
                f"Gemini returned no embeddings (response={response!r})"
            )
        first = embeddings[0]
        if first.values is None:
            raise AIProviderRequestInvalidError(
                f"Gemini returned embedding without values (embedding={first!r})"
            )
        return list(first.values)

    def _translate_error(self, exc: Exception) -> Exception:
        """Gemini SDK / httpx 例外を ``AIProvider*Error`` 階層に翻訳する。

        Stage 4 ``GeminiAssessor._translate_error`` と 1:1 同形 (新規発明しない)。
        マップできなければ ``exc`` をそのまま return (caller である
        ``_embed_once`` が bare re-raise する規約)。

        google-genai 1.x の ``ClientError`` は ``code`` (int HTTP status) と
        ``status`` (gRPC status 文字列、e.g. "INVALID_ARGUMENT") の両方を
        attribute として持つので、両経路を見て robust に判定する。
        """
        # network 系 (httpx は SDK の transport)
        if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
            return AIProviderNetworkError(f"{type(exc).__name__}: {exc}")
        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return AIProviderNetworkError(f"{type(exc).__name__}: {exc}")

        # genai SDK の例外階層 (HTTP status + gRPC status の両方を見る)
        if isinstance(exc, genai_errors.ClientError):
            code = getattr(exc, "code", None)
            status = getattr(exc, "status", None) or ""
            raw_message = str(getattr(exc, "message", "")) or str(exc)
            message = raw_message.lower()

            # red-team chain γ-1: SDK 生 message に key prefix /
            # Authorization header が含まれる経路があるため固定文言に丸める。
            # 詳細 debug は error_chain (SDK class FQN) で代替。
            if "reported as leaked" in message:
                return AIProviderConfigurationError(
                    "Gemini API key has been reported as leaked; rotate immediately"
                )

            if code == 400 or status == "INVALID_ARGUMENT":
                if "api key" in message or "permission" in message:
                    return AIProviderConfigurationError(str(exc))
                if "blocked" in message or "safety" in message:
                    return AIProviderInputRejectedError(str(exc))
                return AIProviderRequestInvalidError(str(exc))
            if code in (401, 403, 404) or status in (
                "UNAUTHENTICATED",
                "PERMISSION_DENIED",
                "NOT_FOUND",
                "FAILED_PRECONDITION",
            ):
                return AIProviderConfigurationError(str(exc))
            if code == 429 or status == "RESOURCE_EXHAUSTED":
                if "quota" in message or "daily" in message:
                    return AIProviderQuotaExhaustedError(str(exc))
                return AIProviderRateLimitedError(str(exc))

        if isinstance(exc, genai_errors.ServerError):
            return AIProviderServiceUnavailableError(str(exc))

        return exc  # bare re-raise (UNKNOWN)
