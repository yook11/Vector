import { Sparkles } from "lucide-react";
import { formatPaperDate } from "@/components/paper";

interface BriefingMastheadProps {
  weekStart: string;
  weekEnd: string;
  totalArticles: number;
}

/** Briefing 一覧ページのマストヘッド。eyebrow / H1 / メタ / 二重罫線 で週次紙面の入口を示す。 */
export function BriefingMasthead({
  weekStart,
  weekEnd,
  totalArticles,
}: BriefingMastheadProps) {
  const weekStartLabel = formatPaperDate(weekStart);
  const weekEndLabel = formatPaperDate(weekEnd);
  return (
    <header>
      {/* top row: eyebrow (左) + メタ群 (右)。二重罫線でタイトルと仕切る */}
      <div className="flex items-center justify-between gap-4 flex-wrap pb-4 mb-[18px] border-b-[3px] border-double border-[var(--vector-ink)]">
        <p
          className="text-[14px] font-semibold uppercase tracking-[0.3em] text-[var(--vector-accent-ink)]"
          style={{ fontFamily: "var(--font-vector-display)" }}
        >
          WEEKLY BRIEFING
        </p>
        <span
          className="inline-flex items-center gap-[14px] flex-wrap text-[12px] tracking-[0.04em] text-[var(--vector-ink-muted)]"
          style={{ fontFamily: "var(--font-vector-maru)" }}
        >
          <span>
            {weekStartLabel} – {weekEndLabel}
          </span>
          <span className="text-[var(--vector-line)]">/</span>
          <span className="inline-flex items-center gap-1.5">
            <Sparkles
              aria-hidden="true"
              className="size-[13px] text-[var(--vector-accent)]"
            />
            今週 {totalArticles} 件を解析
          </span>
        </span>
      </div>

      {/* title row (罫線の下) */}
      <div className="flex items-baseline gap-4 flex-wrap">
        <h1
          className="text-[clamp(28px,3.6vw,40px)] font-extrabold tracking-[0.01em] text-[var(--vector-ink)]"
          style={{ fontFamily: "var(--font-vector-serif)" }}
        >
          今週のブリーフィング
        </h1>
        <span
          className="text-[16px] italic text-[var(--vector-ink-muted)]"
          style={{ fontFamily: "var(--font-vector-display)" }}
        >
          {weekEndLabel} 週
        </span>
      </div>

      {/* description */}
      <p
        className="mt-[10px] text-[13px] text-[var(--vector-ink-muted)]"
        style={{ fontFamily: "var(--font-vector-maru)" }}
      >
        AIが公開ニュースから集約した、分野別の週次解説。
      </p>
    </header>
  );
}
