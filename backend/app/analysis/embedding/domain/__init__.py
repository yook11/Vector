"""embedding BC のドメイン層。

Stage 5 で生成された埋め込みベクトルの VO (``EmbeddingVector``) を表現する。
``EmbeddingVector`` は AI 境界の ``list[float]`` を構造検証 (次元 / 有限性 /
サニティ範囲) して永続化制約 (HALFVEC(768)) を満たすことを型そのもので保証する。
下流 (Repository.save) はこの型を受け取った時点で再検証なしに書き込める
(BC 境界原則: feedback_bc_boundary_guarantees_downstream)。
"""

from app.analysis.embedding.domain.value_objects import (
    EMBEDDING_DIMENSION,
    EmbeddingVector,
)

__all__ = ["EMBEDDING_DIMENSION", "EmbeddingVector"]
