import { ExternalLink } from "lucide-react";
import Link from "next/link";
import { SectionLabel } from "@/components/feedback/SectionLabel";
import { Separator } from "@/components/ui/separator";
import { formatDate } from "@/lib/date";
import { MOCK_ARTICLE } from "../_lib/mock-data";
import { MockWatchlistButton } from "./mock-watchlist-button";

/**
 * 現状の NewsDetail 実装 (NewsDetail.tsx と挙動同一・データのみ MOCK_ARTICLE 差替え)。
 * 比較のために 3 案と同じ page に並べる。
 */
export function NewsDetailBaseline() {
  const a = MOCK_ARTICLE;
  return (
    <div className="relative mx-auto flex max-w-4xl flex-col items-center px-4 py-8 text-center sm:py-12">
      <div className="absolute right-4 top-8 sm:top-12">
        <MockWatchlistButton />
      </div>

      <div className="mb-6 max-w-3xl space-y-4">
        <h1 className="text-2xl font-medium leading-tight text-foreground sm:text-3xl lg:text-4xl">
          {a.translatedTitle}
        </h1>
        <p className="text-sm text-muted-foreground sm:text-base">
          {a.originalTitle}
        </p>
      </div>

      <div className="mb-12 flex flex-wrap items-center justify-center gap-3 text-sm text-muted-foreground">
        <span className="font-medium text-foreground">{a.sourceName}</span>
        <Separator orientation="vertical" className="h-4" />
        <span>{formatDate(a.publishedAt, { withTime: true })}</span>
      </div>

      <div className="mt-4 w-full max-w-2xl space-y-10 border-t border-border pt-12">
        <div className="space-y-4 text-left">
          <SectionLabel as="h2" className="font-semibold">
            AI Summary
          </SectionLabel>
          <p className="text-base leading-relaxed text-foreground">
            {a.summary}
          </p>
        </div>

        <div className="space-y-4 text-left">
          <SectionLabel as="h2" className="font-semibold">
            Investor Take
          </SectionLabel>
          <p className="text-base leading-relaxed text-foreground">
            {a.investorTake}
          </p>
        </div>

        <div className="flex flex-col items-center gap-4 pt-8">
          <Link
            href={a.url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 rounded-full border border-border px-5 py-2.5 text-sm font-medium text-foreground transition-colors hover:bg-accent"
          >
            Read Original Article
            <ExternalLink aria-hidden="true" className="size-3.5" />
          </Link>
          <p className="text-xs text-muted-foreground">
            Analyzed at {formatDate(a.analyzedAt, { withTime: true })}
          </p>
        </div>
      </div>
    </div>
  );
}
