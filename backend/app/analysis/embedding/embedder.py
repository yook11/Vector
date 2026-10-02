"""準備済みGeminiクライアントで記事のベクトルを生成する。"""

from typing import Final

from google.genai.client import AsyncClient
from google.genai.types import EmbedContentConfig

from app.ai_providers.errors import (
    AIProviderResultError,
    AIProviderResultReason,
)
from app.ai_providers.gemini.error_translator import translate_gemini_error
from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.ai.spec import GEMINI_EMBEDDING_SPEC, EmbeddingCallSpec


class GeminiEmbedder(BaseEmbedder):
    """クライアントを借用し、生成・終了と通信設定は呼び出し元に任せる。"""

    SPEC: Final[EmbeddingCallSpec] = GEMINI_EMBEDDING_SPEC

    def __init__(self, *, client: AsyncClient) -> None:
        self._client = client

    @property
    def model_name(self) -> str:
        return self.SPEC.model

    @property
    def dimension(self) -> int:
        return self.SPEC.dimension

    @property
    def provider(self) -> str:
        return self.SPEC.provider

    @property
    def document_prefix(self) -> str:
        return self.SPEC.document_prefix

    async def _call_api(self, text: str) -> list[float]:
        response = await self._client.models.embed_content(
            model=self.SPEC.model,
            contents=text,
            config=EmbedContentConfig(
                output_dimensionality=self.SPEC.output_dimensionality,
                task_type=self.SPEC.task_type,
            ),
        )
        if not response.embeddings:
            raise AIProviderResultError(reason=AIProviderResultReason.EMBEDDINGS_EMPTY)
        values = response.embeddings[0].values
        if values is None:
            raise AIProviderResultError(
                reason=AIProviderResultReason.EMBEDDING_VALUES_MISSING
            )
        return list(values)

    def _translate_error(self, exc: Exception) -> Exception:
        return translate_gemini_error(exc)
