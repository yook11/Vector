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

/** バッジ化された `[[N]]` マーカーを表す mdast ノード。`components` mapping で `SourcePreviewBadge` に差し替える。 */
export interface CitationBadgeNode extends Literal {
  type: "citationBadge";
  data: CitationBadgeData;
}

declare module "mdast" {
  interface PhrasingContentMap {
    citationBadge: CitationBadgeNode;
  }
}

const CITATION_MARKER_PATTERN = /\[\[(\d+)\]\]/g;
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
  /** バッジ化してよい `[[N]]` の ref 一覧 (sources に存在する ref のみ)。 */
  matchableRefs: ReadonlySet<string>;
}

/**
 * 確定回答本文の `[[N]]` マーカーを検出し、sources と一致する ref だけをバッジ用ノードへ変換する remark plugin。
 * 未対応の ref は本文から除去し、link / linkReference の内側は matched / unmatched を問わず除去する
 * (SourcePreviewBadge は button であり、`<a>` 内側に置くと interactive 要素のネストになるため)。
 */
export function remarkCitationMarkers(options: RemarkCitationMarkersOptions) {
  const { matchableRefs } = options;

  return function transformCitationMarkers(tree: Root): undefined {
    findAndReplace(tree, [
      CITATION_MARKER_PATTERN,
      (_marker: string, ref: string, match: RegExpMatchObject) => {
        if (isInsideLink(match.stack)) {
          return null;
        }
        return matchableRefs.has(ref) ? createCitationBadgeNode(ref) : null;
      },
    ]);
    return undefined;
  };
}
