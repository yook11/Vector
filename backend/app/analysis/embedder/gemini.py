"""gemini-embedding-001 を用いた Gemini Embedder 実装。

google-genai SDK の ``embed_content`` API を非同期で呼び出してベクトルを取得する。
``output_dimensionality=768`` を固定で指定し、DB 側 ``HALFVEC(768)`` 列に適合する。

Note:
    Gemini は prefix ではなく ``task_type`` (RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY)
    で文書とクエリの埋め込みを区別する。BaseEmbedder の ``_call_api`` シグネチャは
    task_type を取らないため、``embed_document`` / ``embed_documents`` /
    ``embed_query`` を override し、``_embed_once`` のログ + エラー変換ロジックを
    task_type 対応版で再現する。
"""

from __future__ import annotations

import structlog
from google import genai
from google.genai.errors import APIError, ServerError
from google.genai.types import EmbedContentConfig

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

logger = structlog.get_logger(__name__)


class GeminiEmbedder(BaseEmbedder):
    """BaseEmbedder の gemini-embedding-001 実装。"""

    MODEL = "gemini-embedding-001"
    DIMENSION = 768
    # Gemini API のレート制限値は tier に依存する。確定値が取れないため None で運用し、
    # 429 を structlog でモニタする (RateLimitError として捕捉される)。
    RPM = None
    RPD = None
    DOCUMENT_PREFIX = ""
    QUERY_PREFIX = ""

    def __init__(self) -> None:
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise ConfigurationError("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=api_key)

    # -- 公開 API (override) -------------------------------------------------

    async def embed_document(self, text: str) -> list[float]:
        vectors = await self._embed_with_task(text, task_type="RETRIEVAL_DOCUMENT")
        return vectors[0]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed_with_task(texts, task_type="RETRIEVAL_DOCUMENT")

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self._embed_with_task(text, task_type="RETRIEVAL_QUERY")
        return vectors[0]

    # -- task_type 対応の単発呼び出し ---------------------------------------

    async def _embed_with_task(
        self, contents: str | list[str], *, task_type: str
    ) -> list[list[float]]:
        """``_embed_once`` の task_type 対応版。ログとエラー変換を担う。"""
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
        except AnalysisDomainError:
            raise
        except Exception as exc:
            raise self._translate_error(exc) from exc

    async def _call_api(self, contents: str | list[str]) -> list[list[float]]:
        """BaseEmbedder の抽象シグネチャ遵守。デフォルトは RETRIEVAL_DOCUMENT。

        通常経路は ``embed_document`` / ``embed_query`` から ``_embed_with_task`` を
        経由して呼ばれるため、このメソッドは互換性 fallback として残す。
        """
        return await self._call_gemini(contents, task_type="RETRIEVAL_DOCUMENT")

    async def _call_gemini(
        self, contents: str | list[str], *, task_type: str
    ) -> list[list[float]]:
        """Gemini ``embed_content`` API を呼び出しベクトルのリストを返す。"""
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
            raise ProviderError(
                f"Gemini returned no embeddings (response={response!r})"
            )
        vectors: list[list[float]] = []
        for emb in embeddings:
            if emb.values is None:
                raise ProviderError(
                    f"Gemini returned embedding without values (embedding={emb!r})"
                )
            vectors.append(list(emb.values))
        return vectors

    def _translate_error(self, exc: Exception) -> AnalysisDomainError:
        """Gemini SDK の例外を原因の所在で分類する。"""
        if isinstance(exc, APIError):
            status = exc.status or ""
            message = exc.message or ""

            if "reported as leaked" in message:
                # red-team chain γ-1: SDK message に key prefix が混入する経路
                # を遮断するため固定文言化する。詳細は error_chain で追える。
                return ConfigurationError(
                    "Gemini API key has been reported as leaked; rotate immediately"
                )

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
