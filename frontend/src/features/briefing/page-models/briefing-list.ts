import { listBriefings } from "../api/list-briefings";
import type { BriefingListResponseParsed } from "../schemas/briefing";

/**
 * Briefing 一覧 page の view 状態を JSX 非依存で算出する page-model。
 *
 * ADR-005: page.tsx の async fetch + 分岐判定を pure async 関数に切り出し、
 * vitest の rsc (node) project から直接 unit test 可能にする。
 *
 * 現状は API 側で 11 カテゴリ全部を id 順に返すため identity transform に
 * 近いが、page-model 経路を確立することで将来の display 整形 (formatDate /
 * 週ラベル付与等) を加えても test 経路が変わらない構造になる。
 */
export type BriefingListViewModel = BriefingListResponseParsed;

export async function getBriefingListViewModel(): Promise<BriefingListViewModel> {
  return listBriefings();
}
