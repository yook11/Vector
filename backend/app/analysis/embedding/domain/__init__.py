"""embedding BC のドメイン層。

Stage 3 で生成された埋め込みベクトルの Entity (``Embedding``) と VO
(``EmbeddingVector``) を表現する。``EmbeddingDraft`` は永続化前の
ドメイン入力で、Repository / Service 経由の利用に限定するため、
ここでは re-export しない (fully-qualified import を強制)。
"""

from app.analysis.embedding.domain.embedding import Embedding
from app.analysis.embedding.domain.value_objects import (
    EMBEDDING_DIMENSION,
    EmbeddingVector,
)

__all__ = ["EMBEDDING_DIMENSION", "Embedding", "EmbeddingVector"]
