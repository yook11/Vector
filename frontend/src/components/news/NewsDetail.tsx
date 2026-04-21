import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { sanitizeUrl } from "@/lib/utils";
import type { ArticleDetail as ArticleDetailData, ImpactLevel } from "@/types";

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
    <div className="flex flex-col items-center text-center py-8 sm:py-12 px-4 max-w-4xl mx-auto">
      {/* Top Badges */}
      <div className="flex items-center justify-center gap-2 mb-8">
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
            {article.topic.name}
          </Badge>
        )}
      </div>

      {/* Title Section */}
      <div className="max-w-3xl space-y-4 mb-6">
        <h1 className="text-2xl sm:text-3xl lg:text-4xl font-medium leading-tight text-foreground">
          {article.translatedTitle}
        </h1>
        <p className="text-sm sm:text-base text-muted-foreground">
          {article.original.title}
        </p>
      </div>

      {/* Meta Section */}
      <div className="flex flex-wrap items-center justify-center gap-3 text-[13px] text-muted-foreground mb-12">
        <span className="font-medium text-foreground">
          {article.source.name}
        </span>
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

      {/* AI Analysis Section */}
      <div className="w-full max-w-2xl border-t border-border pt-12 mt-4 space-y-10">
        <div className="space-y-4">
          <h3 className="text-xs uppercase tracking-widest text-muted-foreground font-semibold">
            AI Summary
          </h3>
          <p className="text-[15px] sm:text-base leading-relaxed text-foreground text-left sm:text-center">
            {article.summary}
          </p>
        </div>

        {article.reasoning && (
          <div className="space-y-4">
            <h3 className="text-xs uppercase tracking-widest text-muted-foreground font-semibold">
              Reasoning
            </h3>
            <p className="text-[15px] sm:text-base leading-relaxed text-foreground text-left sm:text-center">
              {article.reasoning}
            </p>
          </div>
        )}

        <div className="pt-8">
          <p className="text-[11px] text-muted-foreground">
            Analyzed at {formatDate(article.analyzedAt)}
          </p>
        </div>
      </div>
    </div>
  );
}
