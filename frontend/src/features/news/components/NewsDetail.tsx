import { ExternalLink } from "lucide-react";
import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { WatchlistButton } from "@/features/watchlist";
import { formatDate } from "@/lib/date";
import { sanitizeUrl } from "@/lib/utils/sanitize-url";
import type { ArticleDetail as ArticleDetailData } from "@/types";

interface NewsDetailProps {
  article: ArticleDetailData;
  /** Pattern B: ウォッチ状態は record の外から注入する。 */
  isWatched: boolean;
}

export function NewsDetail({ article, isWatched }: NewsDetailProps) {
  // --- XSS: validate URL scheme (reject javascript: etc.) ---
  const safeUrl = sanitizeUrl(article.original.url);

  return (
    <div className="relative mx-auto flex max-w-4xl flex-col items-center px-4 py-8 text-center sm:py-12">
      <div className="absolute right-4 top-8 sm:top-12">
        <WatchlistButton articleId={article.id} isWatched={isWatched} />
      </div>

      {/* Top Badges */}
      <div className="mb-8 flex items-center justify-center gap-2">
        {article.topic && (
          <Badge
            variant="secondary"
            className="text-[10px] tracking-widest uppercase px-2.5 py-0.5 truncate max-w-[14rem] bg-muted text-muted-foreground hover:bg-muted/80 border-transparent"
          >
            {article.topic}
          </Badge>
        )}
      </div>

      {/* Title Section */}
      <div className="mb-6 max-w-3xl space-y-4">
        <h1 className="text-2xl font-medium leading-tight text-foreground sm:text-3xl lg:text-4xl">
          {article.translatedTitle}
        </h1>
        <p className="text-sm text-muted-foreground sm:text-base">
          {article.original.title}
        </p>
      </div>

      {/* Meta Section */}
      <div className="mb-12 flex flex-wrap items-center justify-center gap-3 text-[13px] text-muted-foreground">
        <span className="font-medium text-foreground">
          {article.source.name}
        </span>
        <Separator orientation="vertical" className="h-4" />
        <span>{formatDate(article.publishedAt, { withTime: true })}</span>
      </div>

      {/* AI Analysis Section */}
      <div className="mt-4 w-full max-w-2xl space-y-10 border-t border-border pt-12">
        <div className="space-y-4 text-left">
          <h2 className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">
            AI Summary
          </h2>
          <p className="text-[15px] leading-relaxed text-foreground sm:text-base">
            {article.summary}
          </p>
        </div>

        {article.investorTake && (
          <div className="space-y-4 text-left">
            <h2 className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">
              Investor Take
            </h2>
            <p className="text-[15px] leading-relaxed text-foreground sm:text-base">
              {article.investorTake}
            </p>
          </div>
        )}

        <div className="flex flex-col items-center gap-4 pt-8">
          {safeUrl !== null && (
            <Link
              href={safeUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 rounded-full border border-border px-5 py-2.5 text-[13px] font-medium text-foreground transition-colors hover:bg-accent"
            >
              Read Original Article
              <ExternalLink aria-hidden="true" className="h-3.5 w-3.5" />
            </Link>
          )}
          <p className="text-[11px] text-muted-foreground">
            Analyzed at {formatDate(article.analyzedAt, { withTime: true })}
          </p>
        </div>
      </div>
    </div>
  );
}
