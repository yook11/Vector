import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { sanitizeUrl } from "@/lib/utils";
import type { NewsResponse } from "@/types";
import { ImpactScore } from "./ImpactScore";
import { SentimentBadge } from "./SentimentBadge";

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

export function NewsDetail({ article }: { article: NewsResponse }) {
  const { analysis } = article;

  // --- XSS対策 Step 3: URLスキームの検証 ---
  // article.url は外部RSSフィードから取得した値であり、信頼できない。
  // sanitizeUrl で http/https 以外（javascript: 等）を排除する。
  // 不正なURLの場合は null が返り、リンク自体を表示しない。
  const safeUrl = sanitizeUrl(article.url);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold leading-tight">
          {analysis?.title ?? article.titleOriginal}
        </h1>
        {analysis && (
          <p className="mt-2 text-sm text-muted-foreground">
            {article.titleOriginal}
          </p>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-3 text-sm text-muted-foreground">
        <span>{article.source}</span>
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
              {kw.keyword}
            </Badge>
          ))}
        </div>
      )}

      {analysis ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center justify-between">
              <span>AI Analysis</span>
              <div className="flex items-center gap-3">
                <SentimentBadge sentiment={analysis.sentiment} />
                <ImpactScore score={analysis.impactScore} />
              </div>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div>
              <h3 className="text-sm font-semibold mb-1">Summary</h3>
              <p className="text-sm text-muted-foreground">
                {analysis.summary}
              </p>
            </div>

            {analysis.reasoning && (
              <div>
                <h3 className="text-sm font-semibold mb-1">Reasoning</h3>
                <p className="text-sm text-muted-foreground">
                  {analysis.reasoning}
                </p>
              </div>
            )}

            <Separator />

            <p className="text-xs text-muted-foreground">
              Analyzed at {formatDate(analysis.analyzedAt)}
            </p>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="py-8 text-center text-muted-foreground">
            Analysis not yet available for this article.
          </CardContent>
        </Card>
      )}

      {article.content ? (
        <Card>
          <CardHeader>
            <CardTitle>Article Content</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="prose prose-sm max-w-none text-muted-foreground whitespace-pre-line">
              {article.content}
            </div>
            {article.contentFetchedAt && (
              <p className="mt-4 text-xs text-muted-foreground">
                Content fetched at {formatDate(article.contentFetchedAt)}
              </p>
            )}
          </CardContent>
        </Card>
      ) : article.contentFetchedAt ? (
        <Card>
          <CardContent className="py-6 text-center text-muted-foreground text-sm">
            Full content could not be extracted from this article.
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
