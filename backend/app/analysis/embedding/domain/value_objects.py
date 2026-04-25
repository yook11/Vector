"""Embedding BC の値オブジェクト。

``EmbeddingVector`` は Stage 3 の埋め込みベクトルを表す不変の VO。
HALFVEC(768) カラムへの永続化前に次元・有限性・サニティ範囲を構造的に強制する。

``EMBEDDING_DIMENSION`` は VO/DB/embedder 間で共有する次元数の SSoT。
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import ConfigDict, RootModel, field_validator

EMBEDDING_DIMENSION = 768
"""埋め込みベクトルの次元数 (SSoT)。

DB カラム ``HALFVEC(768)`` と embedder 実装の ``DIMENSION`` ClassVar が
この値と一致することを前提とする。
"""

_VECTOR_SANITY_BOUND = 1e4
"""HALFVEC (float16) のオーバーフロー域 (>65504) を弾くサニティ上限。

正規化された埋め込みは通常 [-1, 1] に収まる。サニティとして 1e4 を上限とし、
NaN / inf と組み合わせて構造的に異常値を排除する。
"""


class EmbeddingVector(RootModel[tuple[float, ...]]):
    """埋め込みベクトル VO。

    Invariants (validators で構造的に保証):
    - 長さは ``EMBEDDING_DIMENSION`` (768) 固定
    - 各要素は ``math.isfinite`` を満たす (NaN / ±inf を排除)
    - 各要素は ``[-_VECTOR_SANITY_BOUND, _VECTOR_SANITY_BOUND]`` のサニティ範囲内
    - frozen: 生成後は不変
    """

    model_config = ConfigDict(frozen=True)

    @field_validator("root", mode="before")
    @classmethod
    def _coerce_to_tuple(cls, v: Any) -> Any:
        """list / tuple を tuple へ正規化する (frozen 保証のため)。"""
        if isinstance(v, list):
            return tuple(v)
        return v

    @field_validator("root")
    @classmethod
    def _validate_vector(cls, v: tuple[float, ...]) -> tuple[float, ...]:
        if len(v) != EMBEDDING_DIMENSION:
            msg = (
                f"EmbeddingVector must have exactly {EMBEDDING_DIMENSION} dimensions, "
                f"got {len(v)}"
            )
            raise ValueError(msg)
        for i, x in enumerate(v):
            if not math.isfinite(x):
                msg = f"EmbeddingVector[{i}] must be finite, got {x!r}"
                raise ValueError(msg)
            if not -_VECTOR_SANITY_BOUND <= x <= _VECTOR_SANITY_BOUND:
                msg = (
                    f"EmbeddingVector[{i}] must be within "
                    f"[-{_VECTOR_SANITY_BOUND}, {_VECTOR_SANITY_BOUND}], got {x!r}"
                )
                raise ValueError(msg)
        return v

    def __len__(self) -> int:
        return len(self.root)

    def to_list(self) -> list[float]:
        """DB 永続化など list を要求する境界向けの変換。"""
        return list(self.root)
