import {
  formatPaperDate,
  formatPaperTime,
  getSourceBadge,
} from "./paper-style";

interface PaperBylineProps {
  sourceName: string;
  sourceLabel: string;
  publishedAt?: string | null;
  /** 詳細では発行日に時刻を併記する。 */
  withTime?: boolean;
}

/** 出典イニシャルバッジ + 出典名 + 発行日(+時刻) のインライン出典表記。 */
export function PaperByline({
  sourceName,
  sourceLabel,
  publishedAt,
  withTime = false,
}: PaperBylineProps) {
  const source = getSourceBadge(sourceName);
  const time = withTime ? formatPaperTime(publishedAt) : "";

  return (
    <span className="inline-flex min-w-0 flex-wrap items-center gap-2.5">
      <span
        className="inline-flex size-[18px] shrink-0 items-center justify-center rounded-[4px] text-[9px] font-bold text-white"
        style={{
          backgroundColor: source.color,
          fontFamily: "var(--font-vector-sans)",
        }}
      >
        {source.short}
      </span>
      <span
        className="truncate text-[12.5px] font-medium uppercase tracking-[0.12em] text-[var(--vector-ink-soft)]"
        style={{ fontFamily: "var(--font-vector-display)" }}
      >
        {sourceLabel}
      </span>
      <span aria-hidden="true" className="text-[var(--vector-line)]">
        ·
      </span>
      <time
        className="shrink-0 text-[13px] italic text-[var(--vector-ink-muted)]"
        dateTime={publishedAt ?? undefined}
        style={{ fontFamily: "var(--font-vector-display)" }}
      >
        {formatPaperDate(publishedAt)}
        {time ? ` ${time}` : ""}
      </time>
    </span>
  );
}
