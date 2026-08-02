import type { Literal, Root } from "mdast";
import {
  findAndReplace,
  type RegExpMatchObject,
} from "mdast-util-find-and-replace";

/** バッジ化した引用マーカーの変換先タグ名 (`components` mapping のキーと揃える)。 */
export const CITATION_BADGE_TAG_NAME = "span" as const;
/** バッジ化した引用マーカーの ref を保持する data 属性キー。 */
export const CITATION_BADGE_REF_ATTRIBUTE = "citationRef" as const;

interface CitationBadgeData {
  hName: typeof CITATION_BADGE_TAG_NAME;
  hProperties: Record<typeof CITATION_BADGE_REF_ATTRIBUTE, string>;
}

/** バッジ化された引用マーカーを表す mdast ノード。`components` mapping で `SourcePreviewBadge` に差し替える。 */
export interface CitationBadgeNode extends Literal {
  type: "citationBadge";
  data: CitationBadgeData;
}

declare module "mdast" {
  interface PhrasingContentMap {
    citationBadge: CitationBadgeNode;
  }
}

/**
 * 正準形 `[[1]]` に加えて、モデルが自然に出す `[[1], [5]]` のグループ形を受理する。
 * backend の受理構文 (`app/agent/citation_markers.py`) と一致させること。
 */
const CITATION_MARKER_PATTERN = /\[\[(\d+(?:\],[ \t]*\[\d+)*)\]\]/g;
const CITATION_REF_PATTERN = /\d+/g;
const LINK_NODE_TYPES = new Set(["link", "linkReference"]);

/** text ノードの祖先 (`stack` の末尾は text 自身) に link / linkReference が含まれるか判定する。 */
function isInsideLink(stack: RegExpMatchObject["stack"]): boolean {
  return stack.some((ancestor) => LINK_NODE_TYPES.has(ancestor.type));
}

function createCitationBadgeNode(ref: string): CitationBadgeNode {
  return {
    type: "citationBadge",
    value: ref,
    data: {
      hName: CITATION_BADGE_TAG_NAME,
      hProperties: { [CITATION_BADGE_REF_ATTRIBUTE]: ref },
    },
  };
}

export interface RemarkCitationMarkersOptions {
  /** バッジ化してよい ref 一覧 (sources に存在する ref のみ)。 */
  matchableRefs: ReadonlySet<string>;
}

/**
 * 確定回答本文の引用マーカーを検出し、sources と一致する ref だけをバッジ用ノードへ変換する remark plugin。
 * 1 つのグループ形マーカーは ref の数だけバッジへ展開し、区切り文字は本文に残さない。
 * 未対応の ref は本文から除去し、link / linkReference の内側は matched / unmatched を問わず除去する
 * (SourcePreviewBadge は button であり、`<a>` 内側に置くと interactive 要素のネストになるため)。
 */
export function remarkCitationMarkers(options: RemarkCitationMarkersOptions) {
  const { matchableRefs } = options;

  return function transformCitationMarkers(tree: Root): undefined {
    findAndReplace(tree, [
      CITATION_MARKER_PATTERN,
      (_marker: string, refs: string, match: RegExpMatchObject) => {
        if (isInsideLink(match.stack)) {
          return null;
        }
        // 重複排除は 1 マッチ内に閉じる (別位置の同一 ref は従来どおり各々バッジ化する)。
        const refsInGroup = new Set(refs.match(CITATION_REF_PATTERN) ?? []);
        return [...refsInGroup]
          .filter((ref) => matchableRefs.has(ref))
          .map(createCitationBadgeNode);
      },
    ]);
    return undefined;
  };
}
