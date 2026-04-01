import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ImpactLevel, NewsBrief } from "@/types";
import { WatchlistButton } from "./WatchlistButton";

function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return "Unknown";
  return new Date(dateStr).toLocaleDateString("ja-JP", {
    year: "numeric",
    month: "short",
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

export function NewsCard({ article }: { article: NewsBrief }) {
  return (
    <Card className="border-0 bg-transparent shadow-none">
      <CardHeader className="p-0 pb-2">
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <span className="line-clamp-1">
            {article.sourceName} &middot; {formatDate(article.publishedAt)}
          </span>
        </div>
        <div className="flex items-start justify-between gap-3 mt-1.5">
          <CardTitle className="text-base font-medium leading-snug text-foreground">
            <Link href={`/news/${article.id}`} className="hover:underline">
              {article.translatedTitle}
            </Link>
          </CardTitle>
          <div className="flex items-center gap-1.5 shrink-0 mt-0.5">
            <Badge
              variant="outline"
              className={`text-[11px] border ${impactLevelColors[article.impactLevel]}`}
            >
              {article.impactLevel}
            </Badge>
            <WatchlistButton
              newsArticleId={article.id}
              isWatched={article.isWatched}
            />
          </div>
        </div>
      </CardHeader>
      <CardContent className="p-0 flex flex-col gap-3">
        <p className="text-sm text-muted-foreground leading-relaxed line-clamp-2">
          {article.summary}
        </p>
        {article.keywords.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {article.keywords.map((kw) => (
              <Badge
                key={kw.id}
                variant="secondary"
                className="text-[11px] font-normal"
              >
                {kw.name}
              </Badge>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
