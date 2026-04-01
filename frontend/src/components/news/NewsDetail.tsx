import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { sanitizeUrl } from "@/lib/utils";
import type { ImpactLevel, NewsDetail as NewsDetailData } from "@/types";

function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return "Unknown";
  return new Date(dateStr).toLocaleDateString("ja-JP", {
    year: "numeric",
    month: "long",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

const impactLevelColors: Record<ImpactLevel, string> = {
  low: "bg-slate-100 text-slate-700",
  medium: "bg-blue-100 text-blue-700",
  high: "bg-orange-100 text-orange-700",
  critical: "bg-red-100 text-red-700",
};

export function NewsDetail({ article }: { article: NewsDetailData }) {
  // --- XSS: validate URL scheme (reject javascript: etc.) ---
  const safeUrl = sanitizeUrl(article.original.url);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold leading-tight">
          {article.translatedTitle}
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          {article.original.title}
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-3 text-sm text-muted-foreground">
        <span>{article.sourceName}</span>
        <Separator orientation="vertical" className="h-4" />
        <span>{formatDate(article.publishedAt)}</span>
        {safeUrl !== null && (
          <>
            <Separator orientation="vertical" className="h-4" />
            <Link
              href={safeUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-primary hover:underline"
            >
              Original article
            </Link>
          </>
        )}
      </div>

      {article.keywords.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {article.keywords.map((kw) => (
            <Badge key={kw.id} variant="secondary">
              {kw.name}
            </Badge>
          ))}
        </div>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center justify-between">
            <span>AI Analysis</span>
            <Badge
              variant="outline"
              className={impactLevelColors[article.impactLevel]}
            >
              {article.impactLevel}
            </Badge>
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div>
            <h3 className="text-sm font-semibold mb-1">Summary</h3>
            <p className="text-sm text-muted-foreground">{article.summary}</p>
          </div>

          {article.reasoning && (
            <div>
              <h3 className="text-sm font-semibold mb-1">Reasoning</h3>
              <p className="text-sm text-muted-foreground">
                {article.reasoning}
              </p>
            </div>
          )}

          <Separator />

          <p className="text-xs text-muted-foreground">
            Analyzed at {formatDate(article.analyzedAt)}
          </p>
        </CardContent>
      </Card>

      {article.original.content ? (
        <Card>
          <CardHeader>
            <CardTitle>Article Content</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="prose prose-sm max-w-none text-muted-foreground whitespace-pre-line">
              {article.original.content}
            </div>
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
