import type { MentionTrend } from "@/types/types.gen";
import { type ColumnMode, columnMentionTrends } from "../display";
import { MentionRow } from "./MentionRow";

interface RankingColumnProps {
  mode: ColumnMode;
  mentionTrends: MentionTrend[];
}

const COLUMN_META: Record<
  ColumnMode,
  { en: string; ja: string; note: string }
> = {
  count: {
    en: "Most mentioned",
    ja: "言及数上位",
    note: "記事数順",
  },
  growth: {
    en: "Fastest growing",
    ja: "急上昇ワード",
    note: "伸び率順",
  },
};

/** ランキング1カラム(ColumnHead + 行リスト)。 */
export function RankingColumn({ mode, mentionTrends }: RankingColumnProps) {
  const meta = COLUMN_META[mode];
  const rows = columnMentionTrends(mode, mentionTrends);

  return (
    <div className="flex flex-col">
      {/* ColumnHead */}
      <div className="mb-3 pb-2 border-b-2 border-[var(--vector-ink)]">
        <div className="flex items-baseline gap-2 flex-wrap">
          <span
            className="text-[11px] font-semibold uppercase tracking-[0.22em] text-[var(--vector-accent-ink)]"
            style={{ fontFamily: "var(--font-vector-display)" }}
          >
            {meta.en}
          </span>
          <span
            className="text-[15px] font-bold text-[var(--vector-ink)]"
            style={{ fontFamily: "var(--font-vector-serif)" }}
          >
            {meta.ja}
          </span>
          <span
            className="text-[10.5px] text-[var(--vector-ink-muted)]"
            style={{ fontFamily: "var(--font-vector-maru)" }}
          >
            {meta.note}
          </span>
        </div>
      </div>

      {/* 行リスト */}
      {rows.length === 0 ? (
        <p
          className="py-4 text-[12.5px] italic text-[var(--vector-ink-muted)]"
          style={{ fontFamily: "var(--font-vector-display)" }}
        >
          該当するワードはありません
        </p>
      ) : (
        <ul>
          {rows.map(({ rank, trend }) => (
            <MentionRow
              key={`${trend.type}:${trend.name}`}
              rank={rank}
              mention={trend}
              mode={mode}
            />
          ))}
        </ul>
      )}
    </div>
  );
}
