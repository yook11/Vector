import type { BriefingArticleSummary } from "@/types";

interface ArticleCardProps {
  article: BriefingArticleSummary;
}

export function ArticleCard({ article }: ArticleCardProps) {
  return (
    <a
      href={article.url}
      target="_blank"
      rel="noopener noreferrer"
      className="flex flex-col gap-1 rounded-md border border-border/60 bg-card/50 p-3 transition-colors hover:border-border hover:bg-card"
    >
      <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
        {article.sourceName}
      </span>
      <span className="text-xs text-foreground line-clamp-2">
        {article.titleJa}
      </span>
    </a>
  );
}
