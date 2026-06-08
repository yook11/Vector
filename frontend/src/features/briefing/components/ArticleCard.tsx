import { ArrowUpRight } from "lucide-react";
import { PaperByline } from "@/components/paper";
import type { BriefingArticleSummaryParsed } from "../schemas/briefing";

interface ArticleCardProps {
  article: BriefingArticleSummaryParsed;
}

/** 重要記事の見出し (原文へのリンク) + 出典・公開日のバイライン (紙面様式)。 */
export function ArticleCard({ article }: ArticleCardProps) {
  return (
    <div className="flex flex-col gap-2.5">
      <a
        href={article.url}
        target="_blank"
        rel="noopener noreferrer"
        className="group inline-flex items-start gap-1.5 text-[var(--vector-ink)] transition-colors hover:text-[var(--vector-accent-ink)]"
      >
        <span
          className="text-pretty text-[clamp(17px,1.7vw,21px)] font-bold leading-[1.4]"
          style={{ fontFamily: "var(--font-vector-serif)" }}
        >
          {article.titleJa}
        </span>
        <ArrowUpRight
          aria-hidden="true"
          className="mt-1.5 size-3.5 shrink-0 text-[var(--vector-ink-muted)] transition-colors group-hover:text-[var(--vector-accent)]"
        />
      </a>
      <PaperByline
        sourceName={article.sourceName}
        sourceLabel={article.sourceName}
        publishedAt={article.publishedAt}
      />
    </div>
  );
}
