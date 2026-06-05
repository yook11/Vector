import Link from "next/link";
import type { CSSProperties, ReactNode } from "react";
import type { ArticleBrief } from "@/types/types.gen";
import {
  formatPaperDate,
  getArticleSourceLabel,
  getCategoryKicker,
  getSourceBadge,
} from "./paper-style";

interface PaperArticleCardProps {
  actionSlot?: ReactNode;
  article: ArticleBrief;
}

export function PaperArticleCard({
  actionSlot,
  article,
}: PaperArticleCardProps) {
  const sourceLabel = getArticleSourceLabel(article);
  const source = getSourceBadge(article.source.name);
  const kicker = getCategoryKicker(article.category.slug);
  const kickerVars = {
    "--kc-hue": kicker.hue,
    "--kc-hue-dark": kicker.hueDark,
  } as CSSProperties;

  return (
    <article className="relative flex flex-col border-b border-[color-mix(in_oklab,var(--vector-ink)_13%,transparent)] pb-5">
      <div className="mb-3.5 flex items-center justify-between gap-3">
        <span className="inline-flex min-w-0 items-center gap-2.5">
          <span
            aria-hidden="true"
            className="size-[11px] shrink-0 bg-[linear-gradient(135deg,var(--kc-hue)_0_50%,var(--vector-ink)_50%_100%)] dark:bg-[linear-gradient(135deg,var(--kc-hue-dark)_0_50%,var(--vector-ink)_50%_100%)]"
            style={kickerVars}
          />
          <span
            className="shrink-0 text-[12.5px] font-semibold tracking-[0.22em] text-[var(--vector-ink)]"
            style={{ fontFamily: "var(--font-vector-display)" }}
          >
            {kicker.code}
          </span>
          <span
            className="truncate text-[10px] font-medium tracking-[0.08em] text-[var(--vector-ink-muted)]"
            style={{ fontFamily: "var(--font-vector-maru)" }}
            title={article.category.name}
          >
            {article.category.name}
          </span>
        </span>
        {actionSlot && <div className="-mr-1 shrink-0">{actionSlot}</div>}
      </div>

      <Link href={`/news/${article.id}`} className="group block">
        <h2
          className="line-clamp-3 border-b border-[color-mix(in_oklab,var(--vector-ink)_12%,transparent)] pb-3 text-[20.5px] font-bold leading-[1.44] tracking-[0.005em] text-[var(--vector-ink)] transition-colors group-hover:text-[var(--vector-accent-ink)]"
          style={{ fontFamily: "var(--font-vector-serif)" }}
        >
          {article.translatedTitle}
        </h2>
      </Link>

      <p
        className="mt-3 line-clamp-3 text-[13.5px] font-medium leading-[1.86] text-[var(--vector-ink-soft)]"
        style={{ fontFamily: "var(--font-vector-serif)" }}
      >
        {article.summary}
      </p>

      <div className="mt-auto flex items-center justify-between gap-4 pt-4">
        <span className="inline-flex min-w-0 items-center gap-2">
          <span
            className="inline-flex size-4 shrink-0 items-center justify-center rounded-[3px] text-[8.5px] font-bold text-white"
            style={{
              backgroundColor: source.color,
              fontFamily: "var(--font-vector-sans)",
            }}
          >
            {source.short}
          </span>
          <span
            className="truncate text-[11.5px] font-medium uppercase tracking-[0.12em] text-[color-mix(in_oklab,var(--vector-ink)_80%,transparent)]"
            style={{ fontFamily: "var(--font-vector-display)" }}
          >
            {sourceLabel}
          </span>
        </span>
        <time
          className="shrink-0 text-[12.5px] italic text-[var(--vector-ink-muted)]"
          dateTime={article.publishedAt ?? undefined}
          style={{ fontFamily: "var(--font-vector-display)" }}
        >
          {formatPaperDate(article.publishedAt)}
        </time>
      </div>
    </article>
  );
}
