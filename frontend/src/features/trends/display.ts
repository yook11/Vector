import type { MentionTrend, MentionType } from "@/types/types.gen";

// ---------------------------------------------------------------------------
// 種別表示辞書
// ---------------------------------------------------------------------------

export const MENTION_TYPE_META: Record<
  MentionType,
  { label: string; color: string }
> = {
  company: { label: "企業", color: "#B0852A" },
  product: { label: "製品", color: "#7A5BA8" },
  technology: { label: "技術", color: "#0E9E97" },
  person: { label: "人物", color: "#C04D6E" },
  academic: { label: "研究", color: "#3F84C0" },
  government: { label: "政府", color: "#5B6AB0" },
};

// ---------------------------------------------------------------------------
// 順位の列
// ---------------------------------------------------------------------------

export type ColumnMode = "count" | "growth";

// 各列に出す順位の上限。backend はどちらかの順位がこの値以内の名前をすべて公開する。
export const COLUMN_RANK_LIMIT = 5;

/**
 * 列の順位が上限以内の名前を、順位、同じ順位は名前の順に並べる。
 * 伸び率で順位を付けない名前 (growth.rank が null) は伸び率の列に出さない。
 */
export function columnMentionTrends(
  mode: ColumnMode,
  trends: MentionTrend[],
): Array<{ rank: number; trend: MentionTrend }> {
  return trends
    .flatMap((trend) => {
      const rank =
        mode === "count" ? trend.articleVolume.rank : trend.growth.rank;
      return rank !== null && rank <= COLUMN_RANK_LIMIT
        ? [{ rank, trend }]
        : [];
    })
    .sort(
      (a, b) => a.rank - b.rank || a.trend.name.localeCompare(b.trend.name),
    );
}
