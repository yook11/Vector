import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ImpactLevel, NewsResponse } from "@/types";
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
  low: "bg-slate-100 text-slate-700",
  medium: "bg-blue-100 text-blue-700",
  high: "bg-orange-100 text-orange-700",
  critical: "bg-red-100 text-red-700",
};

export function NewsCard({ article }: { article: NewsResponse }) {
  const { analysis } = article;

  return (
    <Card className="transition-shadow hover:shadow-md">
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="text-base leading-snug">
            <Link href={`/news/${article.id}`} className="hover:underline">
              {analysis?.translatedTitle ?? article.originalTitle}
            </Link>
          </CardTitle>
          <div className="flex items-center gap-1 shrink-0">
            {analysis && (
              <Badge
                variant="outline"
                className={impactLevelColors[analysis.impactLevel]}
              >
                {analysis.impactLevel}
              </Badge>
            )}
            <WatchlistButton
              newsArticleId={article.id}
              isWatched={article.isWatched}
            />
          </div>
        </div>
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <span className="line-clamp-1">
            {article.sourceName} &middot; {formatDate(article.publishedAt)}
          </span>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {analysis && (
          <p className="text-sm text-muted-foreground line-clamp-2">
            {analysis.summary}
          </p>
        )}
        {!analysis && (
          <p className="text-sm text-muted-foreground italic">
            Analysis pending...
          </p>
        )}
        {article.keywords.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {article.keywords.map((kw) => (
              <Badge key={kw.id} variant="secondary" className="text-xs">
                {kw.name}
              </Badge>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
