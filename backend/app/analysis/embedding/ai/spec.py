"""Stage 5 Embedder 群の call spec を SSoT として保持する。

Stage 3 (extraction) / Stage 4 (assessment) と同形の frozen dataclass +
module singleton 設計だが、Stage 5 は prompt template / response schema /
call signature を持たないため、``version`` / ``compute_call_signature`` は
本 Spec には含めない (純粋な call config の固定値集合)。

Embedder は ``SPEC`` class attr 経由で参照し、ClassVar SSoT は廃止する。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.analysis.rate_policy import RatePolicy


@dataclass(frozen=True, slots=True)
class EmbeddingCallSpec:
    """Stage 5 Embedder の 1 回の API call に必要な共通 spec。

    フィールド責務:
    - ``provider`` / ``model``: Layer 2-A 共通の identity
    - ``dimension``: 永続化用 ``EmbeddingVector`` VO / DB ``HALFVEC(N)`` の契約値
    - ``output_dimensionality``: SDK ``EmbedContentConfig`` へ渡す API config 値
      (運用上 ``dimension`` と一致する。テストで等値を担保)
    - ``task_type``: SDK の task hint (``"RETRIEVAL_DOCUMENT"`` 等)
    - ``document_prefix``: 文書埋め込み時の prefix (空なら付与なし)
    - ``rate_policy``: provider × model 単位の rate limit policy
    """

    provider: str
    model: str
    dimension: int
    output_dimensionality: int
    task_type: str
    document_prefix: str
    rate_policy: RatePolicy


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

_GEMINI_PROVIDER: Final[str] = "gemini"
_GEMINI_MODEL: Final[str] = "gemini-embedding-001"
_GEMINI_DIMENSION: Final[int] = 768

GEMINI_EMBEDDING_SPEC: Final[EmbeddingCallSpec] = EmbeddingCallSpec(
    provider=_GEMINI_PROVIDER,
    model=_GEMINI_MODEL,
    dimension=_GEMINI_DIMENSION,
    output_dimensionality=_GEMINI_DIMENSION,
    task_type="RETRIEVAL_DOCUMENT",
    document_prefix="",
    rate_policy=RatePolicy(
        provider=_GEMINI_PROVIDER,
        model=_GEMINI_MODEL,
        rpm=None,
        rpd=None,
    ),
)
