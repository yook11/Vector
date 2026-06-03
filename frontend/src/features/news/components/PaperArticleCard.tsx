import Link from "next/link";
import type { CSSProperties, ReactNode } from "react";
import type { ArticleBrief } from "@/types/types.gen";
import {
  formatPaperDate,
  getArticleSourceLabel,
  getCategoryMarkerImage,
  getCategoryMarkerRotation,
  getSourceBadge,
} from "./paper-style";

interface PaperArticleCardProps {
  actionSlot?: ReactNode;
  article: ArticleBrief;
}

type CategoryStyle = CSSProperties & {
  backgroundImage: string;
};

export function PaperArticleCard({
  actionSlot,
  article,
}: PaperArticleCardProps) {
  const sourceLabel = getArticleSourceLabel(article);
  const source = getSourceBadge(article.source.name);
  const markerImage = getCategoryMarkerImage(article.category.name, article.id);
  const categoryStyle: CategoryStyle = {
    backgroundImage: `url("${markerImage}")`,
  };
  const markerRotation = getCategoryMarkerRotation(article.category.name);

  return (
    <article className="relative flex min-h-56 flex-col border-b border-[color-mix(in_oklab,var(--vector-ink)_13%,transparent)] pb-5">
      <div className="mb-3.5 flex items-center justify-between gap-3">
        <span className="inline-block max-w-full">
          <span
            className="inline-block max-w-full truncate bg-no-repeat px-[0.8em] pt-[0.42em] pb-[0.44em] text-[13px] font-black tracking-[0.06em] text-[var(--vector-ink)] [background-position:0_50%] [background-size:100%_2.5em] [text-shadow:0_0_3px_var(--vector-paper),0_0_1.5px_var(--vector-paper)]"
            style={{
              ...categoryStyle,
              fontFamily: "var(--font-vector-maru)",
              transform: `rotate(${markerRotation}deg)`,
              transformOrigin: "left center",
            }}
            title={article.category.name}
          >
            {article.category.name}
          </span>
        </span>
        {actionSlot && <div className="-mr-1 shrink-0">{actionSlot}</div>}
      </div>

      <Link href={`/news/${article.id}`} className="group block">
        <h2
          className="line-clamp-3 text-[20px] font-bold leading-[1.42] tracking-[0.005em] text-[var(--vector-ink)] transition-colors group-hover:text-[var(--vector-accent-ink)]"
          style={{ fontFamily: "var(--font-vector-serif)" }}
        >
          {article.translatedTitle}
        </h2>
      </Link>

      <p
        className="mt-3 line-clamp-2 text-[12.5px] leading-[1.72] text-[var(--vector-ink-soft)]"
        style={{ fontFamily: "var(--font-vector-sans)" }}
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
