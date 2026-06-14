"""trend_discovery の HALFVEC cast 次元が embedding SSoT と一致する不変条件。

insights BC は analysis embedding domain への production import を増やさないため、
``TrendsRepository`` は ``_EMBEDDING_DIM`` を独立 literal として保持する。同じ物理カラム
(``analyzed_articles.embedding`` = ``HALFVEC(768)``) を cast するため、本テストが
``EMBEDDING_DIMENSION`` との一致を構造的に保証し、片方だけの drift を検出する。
"""

from __future__ import annotations

from app.analysis.embedding.domain.value_objects import EMBEDDING_DIMENSION
from app.insights.trend_discovery.repository import _EMBEDDING_DIM


def test_trend_discovery_halfvec_dim_equals_embedding_dimension() -> None:
    assert _EMBEDDING_DIM == EMBEDDING_DIMENSION
