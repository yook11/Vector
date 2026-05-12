"""gemini-embedding-001 を用いた Gemini Embedder 実装。

google-genai SDK の ``embed_content`` API を非同期で呼び出してベクトルを取得する。
``output_dimensionality=768`` を固定で指定し、DB 側 ``HALFVEC(768)`` 列に適合する。

SDK 例外翻訳は Stage 4 ``GeminiAssessor._translate_error`` と完全同形:
``AIProvider*Error`` 階層 (Layer 2-A、Stage 中立) に翻訳して raise する。Stage 5
marker (``Embedding*Error``) への詰め替えは Service 層 ACL の責務であり、本 class
では行わない。``_translate_error`` は未分類例外を ``exc`` として ``return`` し、
caller の bare re-raise guard 規約 (``BaseEmbedder._embed_once`` /
``_embed_with_task``) に委譲する。

Note:
    Gemini は prefix ではなく ``task_type`` (RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY)
    で文書とクエリの埋め込みを区別する。BaseEmbedder の ``_call_api`` シグネチャは
    task_type を取らないため、``embed_document`` / ``embed_documents`` /
    ``embed_query`` を override し、``_embed_once`` のログ + エラー変換ロジックを
    task_type 対応版で再現する。
"""

from __future__ import annotations

import httpx
import structlog
from google import genai
from google.genai import errors as genai_errors
from google.genai.types import EmbedContentConfig

from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.domain.value_objects import EmbeddingVector
from app.analysis.embedding.errors import EmbeddingError
from app.analysis.errors.provider import (
    AIProviderConfigurationError,
    AIProviderError,
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderQuotaExhaustedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
)
from app.config import settings

logger = structlog.get_logger(__name__)


class GeminiEmbedder(BaseEmbedder):
    """BaseEmbedder の gemini-embedding-001 実装。"""

    MODEL = "gemini-embedding-001"
    DIMENSION = 768
    # Gemini API のレート制限値は tier に依存する。確定値が取れないため None で運用し、
    # 429 を structlog でモニタする (``AIProviderRateLimitedError`` として捕捉される)。
    RPM = None
    RPD = None
    DOCUMENT_PREFIX = ""
    QUERY_PREFIX = ""

    def __init__(self) -> None:
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise AIProviderConfigurationError("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=api_key)

    # -- 公開 API (override) -------------------------------------------------

    async def embed_document(self, text: str) -> EmbeddingVector:
        vectors = await self._embed_with_task(text, task_type="RETRIEVAL_DOCUMENT")
        return self._to_vector(vectors[0])

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed_with_task(texts, task_type="RETRIEVAL_DOCUMENT")

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self._embed_with_task(text, task_type="RETRIEVAL_QUERY")
        return vectors[0]

    # -- task_type 対応の単発呼び出し ---------------------------------------

    async def _embed_with_task(
        self, contents: str | list[str], *, task_type: str
    ) -> list[list[float]]:
        """``_embed_once`` の task_type 対応版。ログとエラー変換を担う。

        Pattern (Stage 4 BaseAssessor._call_once と同形):
        - 既に階層内 (``AIProviderError`` / ``EmbeddingError``) の例外は **素通し**
        - それ以外は ``_translate_error`` 経由で翻訳。同じ exc が返ったら
          ``raise`` (from なし、UNKNOWN として catch-all 経路へ)
        - 翻訳された場合のみ ``raise translated from exc`` で原因連鎖
        """
        try:
            logger.info(
                "embed_api_call",
                model=self.model_name,
                batch_size=len(contents) if isinstance(contents, list) else 1,
                task_type=task_type,
            )
            vectors = await self._call_gemini(contents, task_type=task_type)
            logger.info(
                "embed_api_success",
                model=self.model_name,
                count=len(vectors),
            )
            return vectors
        except (AIProviderError, EmbeddingError):
            raise
        except Exception as exc:
            translated = self._translate_error(exc)
            if translated is exc:
                raise
            raise translated from exc

    async def _call_api(self, contents: str | list[str]) -> list[list[float]]:
        """BaseEmbedder の抽象シグネチャ遵守。デフォルトは RETRIEVAL_DOCUMENT。

        通常経路は ``embed_document`` / ``embed_query`` から ``_embed_with_task`` を
        経由して呼ばれるため、このメソッドは互換性 fallback として残す。
        """
        return await self._call_gemini(contents, task_type="RETRIEVAL_DOCUMENT")

    async def _call_gemini(
        self, contents: str | list[str], *, task_type: str
    ) -> list[list[float]]:
        """Gemini ``embed_content`` API を呼び出しベクトルのリストを返す。

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
            contents=contents,
            config=EmbedContentConfig(
                output_dimensionality=self.DIMENSION,
                task_type=task_type,
            ),
        )
        embeddings = response.embeddings
        if not embeddings:
            raise AIProviderRequestInvalidError(
                f"Gemini returned no embeddings (response={response!r})"
            )
        vectors: list[list[float]] = []
        for emb in embeddings:
            if emb.values is None:
                raise AIProviderRequestInvalidError(
                    f"Gemini returned embedding without values (embedding={emb!r})"
                )
            vectors.append(list(emb.values))
        return vectors

    def _translate_error(self, exc: Exception) -> Exception:
        """Gemini SDK / httpx 例外を ``AIProvider*Error`` 階層に翻訳する。

        Stage 4 ``GeminiAssessor._translate_error`` と 1:1 同形 (新規発明しない)。
        マップできなければ ``exc`` をそのまま return (caller である
        ``_embed_with_task`` が bare re-raise する規約)。

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
