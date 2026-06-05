import Link from "next/link";
import type { ReactNode } from "react";
import type { ArticleBrief } from "@/types/types.gen";
import { PaperKicker } from "./PaperKicker";
import {
  formatPaperDate,
  getArticleSourceLabel,
  getCategoryKicker,
  getSourceBadge,
  kickerCssVars,
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

  return (
    <article className="relative flex flex-col border-b border-[color-mix(in_oklab,var(--vector-ink)_14%,transparent)] pb-6">
      <div className="mb-3.5 flex items-center justify-between gap-3">
        <PaperKicker
          slug={article.category.slug}
          name={article.category.name}
        />
        {actionSlot && <div className="-mr-1 shrink-0">{actionSlot}</div>}
      </div>

      <Link href={`/news/${article.id}`} className="group block">
        <h2
          className="mb-3.5 line-clamp-3 text-[20.5px] font-bold leading-[1.44] tracking-[0.005em] text-[var(--vector-ink)] transition-colors group-hover:text-[var(--vector-accent-ink)]"
          style={{ fontFamily: "var(--font-vector-serif)" }}
        >
          {article.translatedTitle}
        </h2>
      </Link>

      <span
        aria-hidden="true"
        className="mb-[15px] block h-[2.5px] w-[34px] rounded-[2px] bg-[var(--kc-hue)] dark:bg-[var(--kc-hue-dark)]"
        style={kickerCssVars(kicker)}
      />

      <p
        className="mb-4 line-clamp-3 text-[13.5px] font-medium leading-[1.86] text-[var(--vector-ink-soft)]"
        style={{ fontFamily: "var(--font-vector-serif)" }}
      >
        {article.summary}
      </p>

      <div className="mt-auto flex items-center justify-between gap-4">
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
