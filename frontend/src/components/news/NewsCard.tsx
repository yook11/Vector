import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import type { ArticleBrief, ImpactLevel } from "@/types";
import { WatchlistButton } from "./WatchlistButton";

function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return "Unknown";
  return new Date(dateStr).toLocaleDateString("ja-JP", {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

const impactLevelColors: Record<ImpactLevel, string> = {
  low: "bg-neutral-100 text-neutral-500 border-neutral-200 dark:bg-neutral-800/30 dark:text-neutral-400 dark:border-neutral-700/50",
  medium:
    "bg-emerald-50 text-emerald-600 border-emerald-200 dark:bg-emerald-950/40 dark:text-emerald-400 dark:border-emerald-800/50",
  high: "bg-amber-50 text-amber-600 border-amber-200 dark:bg-amber-950/40 dark:text-amber-400 dark:border-amber-800/50",
  critical:
    "bg-red-50 text-red-600 border-red-200 dark:bg-red-950/40 dark:text-red-400 dark:border-red-800/50",
};

export function NewsCard({ article }: { article: ArticleBrief }) {
  return (
    <Card className="flex h-full flex-col border-0 bg-transparent p-0 shadow-none gap-0">
      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <Badge
            variant="outline"
            className={`text-[10px] tracking-widest uppercase px-2 py-0.5 border ${impactLevelColors[article.impactLevel]}`}
          >
            {article.impactLevel}
          </Badge>
          {article.topic && (
            <Badge
              variant="outline"
              className="text-[10px] tracking-widest uppercase px-2 py-0.5 border border-neutral-200 bg-transparent text-neutral-600 dark:border-neutral-700/60 dark:text-neutral-400"
            >
              {article.topic.name}
            </Badge>
          )}
        </div>
        <div className="-mt-1 -mr-1 shrink-0">
          <WatchlistButton
            articleId={article.id}
            isWatched={article.isWatched}
          />
        </div>
      </div>

      <Link href={`/news/${article.id}`} className="group mt-5 block">
        <h3 className="text-[18px] font-medium leading-[1.3] tracking-[-0.01em] text-foreground line-clamp-3 group-hover:text-primary transition-colors">
          {article.translatedTitle}
        </h3>
      </Link>

      <p className="mt-2 text-[13px] leading-relaxed text-muted-foreground line-clamp-2">
        {article.summary}
      </p>

      <div className="mt-auto flex items-center gap-1.5 pt-5 text-[11px] text-muted-foreground">
        <span className="font-medium text-foreground/80">
          {article.source.name}
        </span>
        <span className="text-muted-foreground/50">·</span>
        <span>{formatDate(article.publishedAt)}</span>
      </div>
    </Card>
  );
}
