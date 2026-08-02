/**
 * citation marker 構文の共有テストコーパス (backend の写し)。
 *
 * 正本は `backend/tests/agent/citation_marker_corpus.py` の `CITATION_MARKER_CASES`。
 * `label` は 1:1 で対応させ、受理構文が層をまたいでずれていないことを固定する。
 * backend の `BACKEND_ONLY_CASES` (改行をまたぐ形) は、remarkBreaks が text node を
 * 分割して同じ入力を mdast 上で再現できないため写さない。
 *
 * `refs` は初出順・重複排除。plugin は出現ごとにバッジを作るため、比較時は
 * バッジ列を初出順で重複排除してから突き合わせる。
 * `stripped` はバッジ化後に残る text node を連結したもの。
 */
export interface CitationMarkerCase {
  label: string;
  text: string;
  refs: string[];
  stripped: string;
}

const LONG_REF = "1234567890123456789012345678901234567890";

export const CITATION_MARKER_CASES: CitationMarkerCase[] = [
  // 既存形 (受理): 挙動は 1 つも変えない
  { label: "single", text: "前[[1]]後", refs: ["1"], stripped: "前後" },
  {
    label: "adjacent",
    text: "前[[1]][[2]]後",
    refs: ["1", "2"],
    stripped: "前後",
  },
  {
    label: "first-use-order-deduplicated",
    text: "前[[2]][[1]]中[[2]]後",
    refs: ["2", "1"],
    stripped: "前中後",
  },
  {
    label: "space-separated",
    text: "前[[1]] [[5]]後",
    refs: ["1", "5"],
    stripped: "前 後",
  },
  {
    label: "comma-separated",
    text: "前[[1]], [[5]]後",
    refs: ["1", "5"],
    stripped: "前, 後",
  },
  {
    label: "trailing-bracket",
    text: "前[[1]]][[2]]後",
    refs: ["1", "2"],
    stripped: "前]後",
  },
  {
    label: "quadruple-bracket",
    text: "前[[[[1]]]]後",
    refs: ["1"],
    stripped: "前[[]]後",
  },
  {
    label: "triple-open-bracket",
    text: "前[[[1]]後",
    refs: ["1"],
    stripped: "前[後",
  },
  {
    label: "long-ref",
    text: `前[[${LONG_REF}]]後`,
    refs: [LONG_REF],
    stripped: "前後",
  },
  // 追加形 (受理): グループ形
  {
    label: "group",
    text: "前[[1], [5]]後",
    refs: ["1", "5"],
    stripped: "前後",
  },
  {
    label: "group-without-space",
    text: "前[[1],[5]]後",
    refs: ["1", "5"],
    stripped: "前後",
  },
  {
    label: "group-with-two-spaces",
    text: "前[[1],  [5]]後",
    refs: ["1", "5"],
    stripped: "前後",
  },
  {
    label: "group-with-tab",
    text: "前[[1],\t[5]]後",
    refs: ["1", "5"],
    stripped: "前後",
  },
  {
    label: "group-of-three",
    text: "前[[1], [5], [9]]後",
    refs: ["1", "5", "9"],
    stripped: "前後",
  },
  {
    label: "group-then-single",
    text: "前[[1], [5]][[7]]後",
    refs: ["1", "5", "7"],
    stripped: "前後",
  },
  {
    label: "group-deduplicated",
    text: "前[[1], [1]]後",
    refs: ["1"],
    stripped: "前後",
  },
  // 非受理形: 現行どおり literal のまま残る
  { label: "single-bracket", text: "前[1]後", refs: [], stripped: "前[1]後" },
  {
    label: "non-digit-ref",
    text: "前[[a]]後",
    refs: [],
    stripped: "前[[a]]後",
  },
  {
    label: "fullwidth-digit-ref",
    text: "前[[１２]]後",
    refs: [],
    stripped: "前[[１２]]後",
  },
  {
    label: "unclosed-marker",
    text: "前[[12]後",
    refs: [],
    stripped: "前[[12]後",
  },
  { label: "bare-double-open", text: "前[[後", refs: [], stripped: "前[[後" },
  {
    label: "digits-without-close",
    text: "前[[12後",
    refs: [],
    stripped: "前[[12後",
  },
  {
    label: "internal-style-ref",
    text: "前[[internal-1]]後",
    refs: [],
    stripped: "前[[internal-1]]後",
  },
  // 非受理形: グループの構文から外れる形
  {
    label: "empty-trailing-ref",
    text: "前[[1,]]後",
    refs: [],
    stripped: "前[[1,]]後",
  },
  {
    label: "empty-leading-ref",
    text: "前[[, 5]]後",
    refs: [],
    stripped: "前[[, 5]]後",
  },
  {
    label: "space-before-close",
    text: "前[[1] , [5]]後",
    refs: [],
    stripped: "前[[1] , [5]]後",
  },
  {
    label: "space-inside-ref",
    text: "前[[1 ], [5]]後",
    refs: [],
    stripped: "前[[1 ], [5]]後",
  },
  {
    label: "space-after-open",
    text: "前[[1], [ 5]]後",
    refs: [],
    stripped: "前[[1], [ 5]]後",
  },
  {
    label: "unterminated-group",
    text: "前[[1],後",
    refs: [],
    stripped: "前[[1],後",
  },
  {
    label: "group-without-close",
    text: "前[[1], abc後",
    refs: [],
    stripped: "前[[1], abc後",
  },
  // 不正なグループ候補の直後に正しい marker が重なる形
  {
    label: "failed-group-overlapping-single",
    text: "前[[1], [[2]]後",
    refs: ["2"],
    stripped: "前[[1], 後",
  },
  {
    label: "failed-group-overlapping-group",
    text: "前[[1], [[2], [3]]後",
    refs: ["2", "3"],
    stripped: "前[[1], 後",
  },
  // 非受理形: 内側カンマ形とネスト配列リテラル (設計判断 3)
  {
    label: "inner-comma",
    text: "前[[1, 5]]後",
    refs: [],
    stripped: "前[[1, 5]]後",
  },
  {
    label: "inner-comma-tight",
    text: "前[[1,5]]後",
    refs: [],
    stripped: "前[[1,5]]後",
  },
  {
    label: "nested-array-literal",
    text: "行列は[[1, 2], [3, 4]]です",
    refs: [],
    stripped: "行列は[[1, 2], [3, 4]]です",
  },
];
