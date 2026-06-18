import { formatDate } from "@/lib/date";
import type { Trends } from "@/types";

interface TrendsMastheadProps {
  data: Trends;
}

/** トレンドページのマストヘッド(eyebrow/H1/サブ/メタ行/太罫線)。 */
export function TrendsMasthead({ data }: TrendsMastheadProps) {
  return (
    <header className="mb-8">
      {/* eyebrow */}
      <p
        className="mb-2 text-[11px] font-semibold uppercase tracking-[0.26em] text-[var(--vector-accent-ink)]"
        style={{ fontFamily: "var(--font-vector-display)" }}
      >
        Trends · トレンド
      </p>

      {/* H1 */}
      <h1
        className="text-[clamp(30px,4.4vw,46px)] font-bold leading-[1.2] tracking-[0.01em] text-[var(--vector-ink)]"
        style={{ fontFamily: "var(--font-vector-serif)" }}
      >
        注目ワードと急上昇ワード
      </h1>

      {/* サブ */}
      <p
        className="mt-3 max-w-[42em] text-[15px] leading-[1.7] text-[var(--vector-ink-soft)]"
        style={{ fontFamily: "var(--font-vector-serif)" }}
      >
        この1週間のニュースで多く言及された企業・製品・組織と、前週比で注目度が高まったワードをカテゴリ別に表示します。
      </p>

      {/* メタ行 */}
      <p
        className="mt-3 text-[12px] italic text-[var(--vector-ink-muted)] tracking-[0.04em]"
        style={{ fontFamily: "var(--font-vector-display)" }}
      >
        {formatDate(data.windowStart)} – {formatDate(data.windowEnd)}
        {" / "}
        {data.sourceAnalysisCount} 件の記事から集計
        {" / "}
        最終更新 {formatDate(data.generatedAt, { withTime: true })}
      </p>

      {/* 太罫線 */}
      <div aria-hidden="true" className="mt-5 h-[3px] bg-[var(--vector-ink)]" />
    </header>
  );
}
