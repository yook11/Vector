"""回答本文の citation marker 構文を解釈する。"""

from __future__ import annotations

import re
from collections.abc import Iterable

__all__ = ["parse_citation_refs", "strip_citation_markers"]

# 正準形 `[[1]]` に加えて、モデルが自然に出す `[[1], [5]]` のグループ形を受理する。
# 内側カンマ形 `[[1, 5]]` を受理しないのは、ネストした数値配列リテラルとの衝突を
# 避けるため (spec: agent-citation-marker-grouped-refs-slice.md 設計判断 3)。
_CITATION_GROUP_RE = re.compile(r"\[\[([0-9]+(?:\],[ \t]*\[[0-9]+)*)\]\]")
_REF_RE = re.compile(r"[0-9]+")


def parse_citation_refs(text: str) -> tuple[str, ...]:
    """本文から citation marker の ref を初出順・重複排除で取り出す。"""

    return _ordered_unique(
        ref
        for match in _CITATION_GROUP_RE.finditer(text)
        for ref in _REF_RE.findall(match.group(1))
    )


def strip_citation_markers(text: str) -> str:
    """本文から citation marker を除去する。"""

    return _CITATION_GROUP_RE.sub("", text)


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        result.append(value)
        seen.add(value)
    return tuple(result)
