import { ExternalLink } from "lucide-react";
import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { formatDate } from "@/lib/date";
import { MOCK_ARTICLE } from "../_lib/mock-data";
import { MockWatchlistButton } from "./mock-watchlist-button";

export function NewsDetailOptionA() {
  const a = MOCK_ARTICLE;
  return (
    <article className="relative mx-auto max-w-2xl px-4 py-12 sm:py-16">
      <div className="mb-8 flex items-start justify-between gap-3">
        <Badge
          variant="secondary"
          className="bg-muted text-muted-foreground border-transparent text-xs uppercase tracking-widest px-2.5 py-0.5"
        >
          {a.topic}
        </Badge>
        <MockWatchlistButton />
      </div>

      <header className="mb-10 space-y-3">
        <h1 className="text-3xl font-medium leading-tight tracking-tight text-foreground sm:text-4xl">
          {a.translatedTitle}
        </h1>
        <p className="text-sm text-muted-foreground sm:text-base">
          {a.originalTitle}
        </p>
      </header>

      <div className="mb-14 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground">
        <span className="font-medium text-foreground">{a.sourceName}</span>
        <span aria-hidden="true">·</span>
        <span>{a.author}</span>
        <span aria-hidden="true">·</span>
        <span>{formatDate(a.publishedAt, { withTime: true })}</span>
      </div>

      <section className="space-y-5">
        <header className="space-y-1.5">
          <h2 className="text-base font-semibold tracking-tight text-foreground">
            記事の要約
          </h2>
          <p className="text-xs text-muted-foreground">
            AI が原文を翻訳・要約しています
          </p>
        </header>
        <p className="text-base leading-relaxed text-foreground">{a.summary}</p>
      </section>

      <div className="my-16 border-t border-border/60" aria-hidden="true" />

      <section className="space-y-5">
        <header className="space-y-1.5">
          <h2 className="text-base font-semibold tracking-tight text-foreground">
            Vector の見立て
          </h2>
          <p className="text-xs text-muted-foreground">
            投資・市場視点での編集解釈
          </p>
        </header>
        <p className="text-base leading-relaxed text-foreground">
          {a.investorTake}
        </p>
      </section>

      <footer className="mt-16 flex flex-col items-start gap-4 border-t border-border/60 pt-10">
        <Link
          href={a.url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1.5 rounded-full border border-border px-5 py-2.5 text-sm font-medium text-foreground transition-colors hover:bg-accent"
        >
          原文を読む
          <ExternalLink aria-hidden="true" className="size-3.5" />
        </Link>
        <p className="text-xs text-muted-foreground">
          Analyzed at {formatDate(a.analyzedAt, { withTime: true })}
        </p>
      </footer>
    </article>
  );
}
