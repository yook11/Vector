import type { RankedMention } from "@/types";
import { MentionRow } from "./MentionRow";

type ColumnMode = "count" | "growth";

interface RankingColumnProps {
  mode: ColumnMode;
  mentions: RankedMention[];
}

const COLUMN_META: Record<
  ColumnMode,
  { en: string; ja: string; note: string }
> = {
  count: {
    en: "Most mentioned",
    ja: "よく言及",
    note: "出現回数順",
  },
  growth: {
    en: "Fastest growing",
    ja: "伸びている",
    note: "伸び率順",
  },
};

/** ランキング1カラム(ColumnHead + 行リスト)。 */
export function RankingColumn({ mode, mentions }: RankingColumnProps) {
  const meta = COLUMN_META[mode];

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
      {mentions.length === 0 ? (
        <p
          className="py-4 text-[12.5px] italic text-[var(--vector-ink-muted)]"
          style={{ fontFamily: "var(--font-vector-display)" }}
        >
          該当する固有名はありません
        </p>
      ) : (
        <ul>
          {mentions.map((mention, idx) => (
            <MentionRow
              key={`${mention.type}:${mention.name}`}
              rank={idx + 1}
              mention={mention}
              mode={mode}
            />
          ))}
        </ul>
      )}
    </div>
  );
}
