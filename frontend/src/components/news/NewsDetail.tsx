import { ExternalLink } from "lucide-react";
import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { sanitizeUrl } from "@/lib/utils";
import type { ArticleDetail as ArticleDetailData, ImpactLevel } from "@/types";
import { WatchlistButton } from "./WatchlistButton";

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
  low: "bg-neutral-100 text-neutral-500 border-neutral-200 dark:bg-neutral-800/30 dark:text-neutral-400 dark:border-neutral-700/50",
  medium:
    "bg-emerald-50 text-emerald-600 border-emerald-200 dark:bg-emerald-950/40 dark:text-emerald-400 dark:border-emerald-800/50",
  high: "bg-amber-50 text-amber-600 border-amber-200 dark:bg-amber-950/40 dark:text-amber-400 dark:border-amber-800/50",
  critical:
    "bg-red-50 text-red-600 border-red-200 dark:bg-red-950/40 dark:text-red-400 dark:border-red-800/50",
};

export function NewsDetail({ article }: { article: ArticleDetailData }) {
  // --- XSS: validate URL scheme (reject javascript: etc.) ---
  const safeUrl = sanitizeUrl(article.original.url);

  return (
    <div className="relative mx-auto flex max-w-4xl flex-col items-center px-4 py-8 text-center sm:py-12">
      <div className="absolute right-4 top-8 sm:top-12">
        <WatchlistButton articleId={article.id} isWatched={article.isWatched} />
      </div>

      {/* Top Badges */}
      <div className="mb-8 flex items-center justify-center gap-2">
        <Badge
          variant="outline"
          className={`text-[10px] tracking-widest uppercase px-2.5 py-0.5 border ${impactLevelColors[article.impactLevel]}`}
        >
          {article.impactLevel}
        </Badge>
        {article.topic && (
          <Badge
            variant="secondary"
            className="text-[10px] tracking-widest uppercase px-2.5 py-0.5 bg-neutral-100 text-neutral-600 hover:bg-neutral-200 dark:bg-neutral-800 dark:text-neutral-400 border-transparent"
          >
            {article.topic.labelJa}
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
        <span>{formatDate(article.publishedAt)}</span>
      </div>

      {/* AI Analysis Section */}
      <div className="mt-4 w-full max-w-2xl space-y-10 border-t border-border pt-12">
        <div className="space-y-4 text-left">
          <h3 className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">
            AI Summary
          </h3>
          <p className="text-[15px] leading-relaxed text-foreground sm:text-base">
            {article.summary}
          </p>
        </div>

        {article.reasoning && (
          <div className="space-y-4 text-left">
            <h3 className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">
              Reasoning
            </h3>
            <p className="text-[15px] leading-relaxed text-foreground sm:text-base">
              {article.reasoning}
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
              Read original article
              <ExternalLink className="h-3.5 w-3.5" />
            </Link>
          )}
          <p className="text-[11px] text-muted-foreground">
            Analyzed at {formatDate(article.analyzedAt)}
          </p>
        </div>
      </div>
    </div>
  );
}
