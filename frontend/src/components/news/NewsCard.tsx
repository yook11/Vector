import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import type { ArticleBrief } from "@/types";
import { WatchlistButton } from "./WatchlistButton";

function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return "Unknown";
  return new Date(dateStr).toLocaleDateString("ja-JP", {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

export function NewsCard({ article }: { article: ArticleBrief }) {
  return (
    <Card className="flex h-full flex-col border-0 bg-transparent p-0 shadow-none gap-0">
      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          {article.topic && (
            <Badge
              variant="outline"
              className="text-[10px] tracking-widest uppercase px-2 py-0.5 truncate max-w-[14rem] border border-neutral-200 bg-transparent text-neutral-600 dark:border-neutral-700/60 dark:text-neutral-400"
            >
              {article.topic}
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
