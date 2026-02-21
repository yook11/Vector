import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { SentimentBadge } from "./SentimentBadge";
import { ImpactScore } from "./ImpactScore";
import type { NewsResponse } from "@/types";

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

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold leading-tight">
          {analysis?.titleJa ?? article.titleOriginal}
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
        <Separator orientation="vertical" className="h-4" />
        <Link
          href={article.url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-primary hover:underline"
        >
          Original article
        </Link>
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
                {analysis.summaryJa}
              </p>
            </div>

            {analysis.keyTopics && analysis.keyTopics.length > 0 && (
              <div>
                <h3 className="text-sm font-semibold mb-1">Key Topics</h3>
                <div className="flex flex-wrap gap-1">
                  {analysis.keyTopics.map((topic) => (
                    <Badge key={topic} variant="outline">
                      {topic}
                    </Badge>
                  ))}
                </div>
              </div>
            )}

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
              Analyzed by {analysis.aiProvider} at{" "}
              {formatDate(analysis.analyzedAt)}
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
