import type { PendingCategory } from "../page-models/briefing-list";

interface BriefingPendingRowProps {
  pending: PendingCategory[];
}

/** 未生成カテゴリを「準備中」チップ一覧で表示。データ待ちを静かに可視化する。 */
export function BriefingPendingRow({ pending }: BriefingPendingRowProps) {
  if (pending.length === 0) return null;

  return (
    <section className="mt-[44px]">
      {/* heading row */}
      <div className="flex items-center gap-3 mb-4">
        <span
          className="text-[12px] font-semibold uppercase tracking-[0.24em] text-[var(--vector-ink-muted)]"
          style={{ fontFamily: "var(--font-vector-display)" }}
        >
          準備中
        </span>
        <span
          className="flex-1 h-px bg-[var(--vector-line)]"
          aria-hidden="true"
        />
      </div>

      {/* chips */}
      <div className="flex flex-wrap gap-x-3 gap-y-2.5">
        {pending.map((cat) => (
          <span
            key={cat.id}
            className="inline-flex items-center gap-[9px] rounded-full border border-dashed border-[var(--vector-line)] bg-[var(--vector-surface-2)] px-[15px] py-[7px]"
          >
            <span
              aria-hidden="true"
              className="size-2 shrink-0 bg-[var(--vector-line)]"
            />
            <span
              className="text-[12.5px] text-[var(--vector-ink-muted)]"
              style={{ fontFamily: "var(--font-vector-maru)" }}
            >
              {cat.name}
            </span>
            <span
              className="text-[11px] italic text-[var(--vector-ink-muted)] opacity-80"
              style={{ fontFamily: "var(--font-vector-display)" }}
            >
              近日
            </span>
          </span>
        ))}
      </div>
    </section>
  );
}
