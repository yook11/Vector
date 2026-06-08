import { listBriefings } from "../api/list-briefings";

/**
 * Briefing 一覧 page の view 状態を JSX 非依存で算出する page-model。
 *
 * ADR-005: page.tsx の async fetch + 整形を pure async 関数へ切り出し、
 * vitest の rsc (node) project から直接 unit test 可能にする。
 *
 * 責務:
 * - 生成済 (latest あり) と未生成 (latest なし) を ready / pending に分割する
 *   (案H は ready をバンドカード、pending を「準備中」チップで別表示する)。
 * - 週の終端日 (weekEnd = weekStart + 6 日) を導出し masthead の週レンジに使う。
 * backend の Category.id 昇順は維持する (frontend で sort しない)。
 */

export interface BriefingCardCategory {
  id: number;
  slug: string;
  name: string;
}

export interface ReadyBriefingCard {
  category: BriefingCardCategory;
  weekStart: string;
  headline: string;
  summary: string;
  inputArticleCount: number;
}

export interface PendingCategory {
  id: number;
  name: string;
}

export interface BriefingListViewModel {
  weekStart: string; // currentWeekStart (週初・月曜)
  weekEnd: string; // weekStart + 6 日 (日曜) を導出
  totalArticles: number;
  ready: ReadyBriefingCard[];
  pending: PendingCategory[];
}

/** ISO date (YYYY-MM-DD) に日数を足す。UTC 正午基準で日跨ぎの TZ ドリフトを避ける。 */
function addDaysIso(iso: string, days: number): string {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

export async function getBriefingListViewModel(): Promise<BriefingListViewModel> {
  const data = await listBriefings();

  const ready: ReadyBriefingCard[] = [];
  const pending: PendingCategory[] = [];
  for (const item of data.items) {
    if (item.latest === null) {
      pending.push({ id: item.category.id, name: item.category.name });
    } else {
      ready.push({
        category: item.category,
        weekStart: item.latest.weekStart,
        headline: item.latest.headline,
        summary: item.latest.summary,
        inputArticleCount: item.latest.inputArticleCount,
      });
    }
  }

  return {
    weekStart: data.currentWeekStart,
    weekEnd: addDaysIso(data.currentWeekStart, 6),
    totalArticles: data.totalArticles,
    ready,
    pending,
  };
}
