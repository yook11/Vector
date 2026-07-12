"""Internal search AI adapters."""

from app.agent.evidence_collection.internal_search.ai.gemini import GeminiQueryEmbedder
from app.agent.evidence_collection.internal_search.ai.gemini_spec import (
    GEMINI_QUERY_EMBEDDING_SPEC,
    QueryEmbeddingCallSpec,
)

__all__ = [
    "GEMINI_QUERY_EMBEDDING_SPEC",
    "GeminiQueryEmbedder",
    "QueryEmbeddingCallSpec",
]
