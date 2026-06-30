"""Gemini query embedding call spec for internal retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.analysis.embedding.domain.value_objects import EMBEDDING_DIMENSION
from app.analysis.rate_limit import AIModelRateLimitPolicy


@dataclass(frozen=True, slots=True)
class QueryEmbeddingCallSpec:
    """Provider call config for query embeddings."""

    provider: str
    model: str
    dimension: int
    output_dimensionality: int
    task_type: str
    rate_limit_policy: AIModelRateLimitPolicy


_GEMINI_PROVIDER: Final[str] = "gemini"
_GEMINI_MODEL: Final[str] = "gemini-embedding-001"

GEMINI_QUERY_EMBEDDING_SPEC: Final[QueryEmbeddingCallSpec] = QueryEmbeddingCallSpec(
    provider=_GEMINI_PROVIDER,
    model=_GEMINI_MODEL,
    dimension=EMBEDDING_DIMENSION,
    output_dimensionality=EMBEDDING_DIMENSION,
    task_type="RETRIEVAL_QUERY",
    rate_limit_policy=AIModelRateLimitPolicy(
        provider=_GEMINI_PROVIDER,
        model=_GEMINI_MODEL,
        rules=(),
    ),
)

_IDENTITY_SEPARATOR: Final[str] = ":"


def embedder_identity_of(spec: QueryEmbeddingCallSpec) -> str:
    """embedder の出力を変える全軸を区切り付きで符号化した同一性キー。

    query 埋め込みキャッシュのキーに含め、model / task_type / 次元が変わった行を
    別空間として扱わせる (別条件のベクトルを誤再利用する stale hit を防ぐ)。構成
    要素に区切り文字が混入すると別 spec と衝突しうるため拒否する。
    """

    parts = (
        spec.provider,
        spec.model,
        spec.task_type,
        str(spec.dimension),
        str(spec.output_dimensionality),
    )
    for part in parts:
        if _IDENTITY_SEPARATOR in part:
            raise ValueError(
                f"embedder identity component must not contain "
                f"{_IDENTITY_SEPARATOR!r}: {part!r}"
            )
    return _IDENTITY_SEPARATOR.join(parts)
