"""embedding BC のドメイン層。

Stage 5 で生成された埋め込みベクトルの VO (``EmbeddingVector``) を表現する。
``EmbeddingDraft`` は永続化前のドメイン入力で、Repository / Service 経由の
利用に限定するため、ここでは re-export しない (fully-qualified import を強制)。
"""

from app.analysis.embedding.domain.value_objects import (
    EMBEDDING_DIMENSION,
    EmbeddingVector,
)

__all__ = ["EMBEDDING_DIMENSION", "EmbeddingVector"]
