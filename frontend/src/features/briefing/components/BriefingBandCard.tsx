import { ArrowUpRight } from "lucide-react";
import { PendingAwareLink } from "@/components/layout/PageNavigation";
import {
  formatPaperDate,
  getCategoryKicker,
  kickerCssVars,
} from "@/components/paper";
import type { ReadyBriefingCard } from "../page-models/briefing-list";

interface BriefingBandCardProps {
  card: ReadyBriefingCard;
  currentWeekStart: string;
}

/** カテゴリ別 briefing カード。バンドヘッダ (カテゴリ色) + 本文 (見出し/要約) の2段構成。 */
export function BriefingBandCard({
  card,
  currentWeekStart,
}: BriefingBandCardProps) {
  const kicker = getCategoryKicker(card.category.slug);

  return (
    <PendingAwareLink
      href={`/briefing/${card.category.slug}`}
      className="group block no-underline overflow-hidden rounded-[3px_3px_12px_12px] border border-[var(--vector-line)] bg-[var(--vector-surface)] border-t-[3px] border-t-[var(--kc-hue)] dark:border-t-[var(--kc-hue-dark)] transition-[transform,box-shadow,border-color] duration-150 ease-out hover:-translate-y-0.5 hover:border-[var(--vector-ink-muted)] hover:shadow-[0_18px_44px_-30px_rgba(20,16,10,0.55)]"
      style={kickerCssVars(kicker)}
    >
      {/* band header */}
      <div className="flex items-center gap-3 px-[clamp(20px,2.4vw,28px)] py-[11px] bg-[color-mix(in_srgb,var(--kc-hue)_9%,transparent)] dark:bg-[color-mix(in_srgb,var(--kc-hue-dark)_18%,transparent)] border-b border-[color-mix(in_srgb,var(--kc-hue)_15%,transparent)] dark:border-[color-mix(in_srgb,var(--kc-hue-dark)_30%,transparent)]">
        {/* split-dot */}
        <span
          aria-hidden="true"
          className="size-[11px] shrink-0 bg-[linear-gradient(135deg,var(--kc-hue)_0_50%,var(--vector-ink)_50%_100%)] dark:bg-[linear-gradient(135deg,var(--kc-hue-dark)_0_50%,var(--vector-ink)_50%_100%)]"
        />
        {/* code */}
        <span
          className="text-[11.5px] font-semibold tracking-[0.2em] whitespace-nowrap text-[var(--kc-hue)] dark:text-[var(--kc-hue-dark)]"
          style={{ fontFamily: "var(--font-vector-display)" }}
        >
          {kicker.code}
        </span>
        {/* category name */}
        <span
          className="text-[15px] font-bold whitespace-nowrap text-[var(--vector-ink)]"
          style={{ fontFamily: "var(--font-vector-serif)" }}
        >
          {card.category.name}
        </span>
        {/* stale-week label (古い週のみ。ISO 日付文字列の辞書順 = 時系列順) */}
        {card.weekStart < currentWeekStart && (
          <span
            className="text-[11px] italic text-[var(--vector-ink-muted)]"
            style={{ fontFamily: "var(--font-vector-display)" }}
          >
            {formatPaperDate(card.weekStart)} 週
          </span>
        )}
        {/* article count (右端) */}
        <span
          className="ml-auto whitespace-nowrap italic text-[13px] text-[var(--vector-ink-muted)]"
          style={{ fontFamily: "var(--font-vector-display)" }}
        >
          <span className="text-[15px] font-semibold not-italic text-[var(--vector-accent-ink)]">
            {card.inputArticleCount}
          </span>
          件
        </span>
      </div>

      {/* body */}
      <div className="flex flex-col gap-[10px] px-[clamp(20px,2.4vw,28px)] pt-[clamp(17px,1.9vw,22px)] pb-[clamp(16px,1.8vw,20px)]">
        <h3
          className="text-[clamp(19px,2vw,23px)] font-extrabold leading-[1.4] text-[var(--vector-ink)] max-w-[30em] line-clamp-2 group-hover:underline underline-offset-4 decoration-1"
          style={{ fontFamily: "var(--font-vector-serif)" }}
        >
          {card.headline}
        </h3>
        <p
          className="text-[clamp(14px,1.5vw,15.5px)] font-medium leading-[1.85] text-[var(--vector-ink-soft)] max-w-[52em] line-clamp-2"
          style={{ fontFamily: "var(--font-vector-serif)" }}
        >
          {card.summary}
        </p>
        {/* footer */}
        <div className="flex items-center justify-end pt-2">
          <span
            className="text-[13px] text-[var(--vector-accent-ink)] inline-flex items-center gap-1"
            style={{ fontFamily: "var(--font-vector-maru)" }}
          >
            読む
            <ArrowUpRight aria-hidden="true" className="size-[15px]" />
          </span>
        </div>
      </div>
    </PendingAwareLink>
  );
}
