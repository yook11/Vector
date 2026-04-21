import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
    <Card className="relative flex flex-col items-center text-center border-0 bg-transparent shadow-none px-4 py-8 h-full sm:px-6">
      <div className="absolute top-4 right-4 sm:top-6 sm:right-6">
        <WatchlistButton
          articleId={article.id}
          isWatched={article.isWatched}
        />
      </div>

      {/* Top Tags area */}
      <div className="flex items-center justify-center gap-2 mb-6 h-6">
        <Badge
          variant="outline"
          className={`text-[10px] tracking-widest uppercase px-2.5 py-0.5 border ${impactLevelColors[article.impactLevel]}`}
        >
          {article.impactLevel}
        </Badge>
        {article.topic && (
          <Badge variant="secondary" className="text-[10px] tracking-widest uppercase px-2.5 py-0.5 bg-neutral-100 text-neutral-600 hover:bg-neutral-200 dark:bg-neutral-800 dark:text-neutral-400 border-transparent">
            {article.topic.name}
          </Badge>
        )}
      </div>

      <CardHeader className="p-0 max-w-[280px] flex-grow flex flex-col justify-start items-center">
        <CardTitle className="text-[15px] sm:text-base font-medium leading-snug text-foreground pb-4 hover:text-primary transition-colors">
          <Link href={`/news/${article.id}`}>
            {article.translatedTitle}
          </Link>
        </CardTitle>
        <p className="text-[13px] text-muted-foreground line-clamp-3 leading-relaxed">
          {article.summary}
        </p>
      </CardHeader>
      
      <CardContent className="p-0 w-full flex flex-col items-center mt-6">
        <p className="text-[13px] font-medium text-foreground mb-1">
          {article.source.name}
        </p>
        <p className="text-[12px] text-muted-foreground">
          {formatDate(article.publishedAt)}
        </p>
      </CardContent>
    </Card>
  );
}
