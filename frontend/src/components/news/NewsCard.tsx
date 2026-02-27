import Link from "next/link";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { CategoryBadge } from "./CategoryBadge";
import { SentimentBadge } from "./SentimentBadge";
import { ImpactScore } from "./ImpactScore";
import { WatchlistButton } from "./WatchlistButton";
import type { NewsResponse } from "@/types";

function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return "Unknown";
  return new Date(dateStr).toLocaleDateString("ja-JP", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export function NewsCard({ article }: { article: NewsResponse }) {
  const { analysis } = article;

  return (
    <Card className="transition-shadow hover:shadow-md">
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="text-base leading-snug">
            <Link
              href={`/news/${article.id}`}
              className="hover:underline"
            >
              {analysis?.title ?? article.titleOriginal}
            </Link>
          </CardTitle>
          <div className="flex items-center gap-1 shrink-0">
            {analysis && <ImpactScore score={analysis.impactScore} />}
            <WatchlistButton
              newsArticleId={article.id}
              isWatched={article.isWatched}
            />
          </div>
        </div>
        <p className="text-xs text-muted-foreground line-clamp-1">
          {article.source} &middot; {formatDate(article.publishedAt)}
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        {analysis && (
          <>
            <p className="text-sm text-muted-foreground line-clamp-2">
              {analysis.summary}
            </p>
            <div className="flex flex-wrap gap-1">
              <SentimentBadge sentiment={analysis.sentiment} />
              {analysis.investmentCategories?.map((cat) => (
                <CategoryBadge key={cat.slug} category={cat} />
              ))}
            </div>
          </>
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
                {kw.keyword}
              </Badge>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
