import type { WeeklyTrendsResponse } from "@/types";
import { getWeeklyTrends } from "../api/get-weekly-trends";

/**
 * Weekly Trends page の view 状態を JSX 非依存で算出する page-model。
 *
 * ADR-005 (RSC ユニットテスト戦略) の page-models pattern 第一号。
 * page.tsx の async fetch + 分岐判定を pure async 関数に切り出して、
 * vitest の rsc (node) project から直接 unit test 可能にする。
 *
 * 現状は API 側で discriminated union を返すため identity transform に近いが、
 * page-model 経路を確立することで将来の display 整形 (formatDate 等) や
 * 補助 fetch を加えても test 経路が変わらない構造になる。
 */
export type WeeklyTrendsViewModel = WeeklyTrendsResponse;

export async function getWeeklyTrendsViewModel(): Promise<WeeklyTrendsViewModel> {
  return getWeeklyTrends();
}
