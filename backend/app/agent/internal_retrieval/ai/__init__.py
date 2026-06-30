"""Internal retrieval AI adapters."""

from app.agent.internal_retrieval.ai.gemini import GeminiQueryEmbedder
from app.agent.internal_retrieval.ai.gemini_spec import (
    GEMINI_QUERY_EMBEDDING_SPEC,
    QueryEmbeddingCallSpec,
)

__all__ = [
    "GEMINI_QUERY_EMBEDDING_SPEC",
    "GeminiQueryEmbedder",
    "QueryEmbeddingCallSpec",
]
