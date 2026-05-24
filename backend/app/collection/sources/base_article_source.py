"""``BaseArticleSource`` — ``in_scope`` / ``select`` の default を供給する mixin。

``ArticleSource`` は ``@runtime_checkable`` の構造的 Protocol で、Protocol の
method body は非継承 class には生えない。よって「単純 source は ``read`` +
``map_entry`` の 2 つだけ書けばよい」を実現するため、default は本 mixin の
classmethod で供給し、各 source が継承する。``read`` / ``map_entry`` は **あえて
持たせない** (未実装の source は ``isinstance(ArticleSource)`` が False になり
登録ガードで気づける)。
"""

from __future__ import annotations

from typing import TypeVar

T = TypeVar("T")


class BaseArticleSource:
    """全 source が継承する default 供給 mixin。"""

    @classmethod
    def in_scope(cls, entry: object) -> bool:  # noqa: ARG003
        """default: 全件を収集スコープとして採用する。"""
        return True

    @classmethod
    def select(cls, entries: list[T]) -> list[T]:
        """default: 恒等 (整序・制限・dedup を行わない)。"""
        return entries
