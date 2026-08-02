"""citation marker 構文の共有テストコーパス。

`backend/specs/agent-citation-marker-grouped-refs-slice.md` の受理例・非受理例を
1 箇所に固定する。正規表現 (`app.agent.citation_markers`) と状態機械
(`app.agent.answering.visible_text`) は同じコーパスに対して同じ ref 列・同じ
可視テキストを返さなければならない。frontend の remark plugin も同じ組を写す。

各 `text` は非空白文字で始まり非空白文字で終わる。状態機械は外側の空白を捨てる
ため、この条件を満たす入力に限って「marker 除去後の本文」が両実装で一致する。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "BACKEND_ONLY_CASES",
    "CITATION_MARKER_CASES",
    "CitationMarkerCase",
]


@dataclass(frozen=True, slots=True)
class CitationMarkerCase:
    label: str
    text: str
    refs: tuple[str, ...]
    stripped: str


_LONG_REF = "1234567890123456789012345678901234567890"


CITATION_MARKER_CASES: tuple[CitationMarkerCase, ...] = (
    # 既存形 (受理): 挙動は 1 つも変えない
    CitationMarkerCase("single", "前[[1]]後", ("1",), "前後"),
    CitationMarkerCase("adjacent", "前[[1]][[2]]後", ("1", "2"), "前後"),
    CitationMarkerCase(
        "first-use-order-deduplicated",
        "前[[2]][[1]]中[[2]]後",
        ("2", "1"),
        "前中後",
    ),
    CitationMarkerCase("space-separated", "前[[1]] [[5]]後", ("1", "5"), "前 後"),
    CitationMarkerCase("comma-separated", "前[[1]], [[5]]後", ("1", "5"), "前, 後"),
    CitationMarkerCase("trailing-bracket", "前[[1]]][[2]]後", ("1", "2"), "前]後"),
    CitationMarkerCase("quadruple-bracket", "前[[[[1]]]]後", ("1",), "前[[]]後"),
    CitationMarkerCase("triple-open-bracket", "前[[[1]]後", ("1",), "前[後"),
    CitationMarkerCase("long-ref", f"前[[{_LONG_REF}]]後", (_LONG_REF,), "前後"),
    # 追加形 (受理): グループ形
    CitationMarkerCase("group", "前[[1], [5]]後", ("1", "5"), "前後"),
    CitationMarkerCase("group-without-space", "前[[1],[5]]後", ("1", "5"), "前後"),
    CitationMarkerCase("group-with-two-spaces", "前[[1],  [5]]後", ("1", "5"), "前後"),
    CitationMarkerCase("group-with-tab", "前[[1],\t[5]]後", ("1", "5"), "前後"),
    CitationMarkerCase(
        "group-of-three", "前[[1], [5], [9]]後", ("1", "5", "9"), "前後"
    ),
    CitationMarkerCase(
        "group-then-single",
        "前[[1], [5]][[7]]後",
        ("1", "5", "7"),
        "前後",
    ),
    CitationMarkerCase("group-deduplicated", "前[[1], [1]]後", ("1",), "前後"),
    # 非受理形: 現行どおり literal のまま残る
    CitationMarkerCase("single-bracket", "前[1]後", (), "前[1]後"),
    CitationMarkerCase("non-digit-ref", "前[[a]]後", (), "前[[a]]後"),
    CitationMarkerCase("fullwidth-digit-ref", "前[[１２]]後", (), "前[[１２]]後"),
    CitationMarkerCase("unclosed-marker", "前[[12]後", (), "前[[12]後"),
    CitationMarkerCase("bare-double-open", "前[[後", (), "前[[後"),
    CitationMarkerCase("digits-without-close", "前[[12後", (), "前[[12後"),
    CitationMarkerCase(
        "internal-style-ref",
        "前[[internal-1]]後",
        (),
        "前[[internal-1]]後",
    ),
    # 非受理形: グループの構文から外れる形
    CitationMarkerCase("empty-trailing-ref", "前[[1,]]後", (), "前[[1,]]後"),
    CitationMarkerCase("empty-leading-ref", "前[[, 5]]後", (), "前[[, 5]]後"),
    CitationMarkerCase("space-before-close", "前[[1] , [5]]後", (), "前[[1] , [5]]後"),
    CitationMarkerCase("space-inside-ref", "前[[1 ], [5]]後", (), "前[[1 ], [5]]後"),
    CitationMarkerCase("space-after-open", "前[[1], [ 5]]後", (), "前[[1], [ 5]]後"),
    CitationMarkerCase("unterminated-group", "前[[1],後", (), "前[[1],後"),
    CitationMarkerCase("group-without-close", "前[[1], abc後", (), "前[[1], abc後"),
    # 不正なグループ候補の直後に正しい marker が重なる形。候補の末尾 `[` を次の
    # marker の開き括弧として拾い直せないと、既存の `[[2]]` が除去されなくなる。
    CitationMarkerCase(
        "failed-group-overlapping-single",
        "前[[1], [[2]]後",
        ("2",),
        "前[[1], 後",
    ),
    CitationMarkerCase(
        "failed-group-overlapping-group",
        "前[[1], [[2], [3]]後",
        ("2", "3"),
        "前[[1], 後",
    ),
    # 非受理形: 内側カンマ形とネスト配列リテラル (設計判断 3)
    CitationMarkerCase("inner-comma", "前[[1, 5]]後", (), "前[[1, 5]]後"),
    CitationMarkerCase("inner-comma-tight", "前[[1,5]]後", (), "前[[1,5]]後"),
    CitationMarkerCase(
        "nested-array-literal",
        "行列は[[1, 2], [3, 4]]です",
        (),
        "行列は[[1, 2], [3, 4]]です",
    ),
)


BACKEND_ONLY_CASES: tuple[CitationMarkerCase, ...] = (
    # 改行は区切りに含めない。frontend では remarkBreaks が text node を分割するため
    # 同じ入力を mdast 経由で再現できず、backend 側だけで固定する。
    CitationMarkerCase(
        "group-across-newline",
        "前[[1],\n[5]]後",
        (),
        "前[[1],\n[5]]後",
    ),
)
