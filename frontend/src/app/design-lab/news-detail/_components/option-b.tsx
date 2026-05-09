import { Compass, ExternalLink, FileText } from "lucide-react";
import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { formatDate } from "@/lib/date";
import { MOCK_ARTICLE } from "../_lib/mock-data";
import { MockWatchlistButton } from "./mock-watchlist-button";

function paragraphs(text: string): string[] {
  return text
    .split(/\n\n+/)
    .map((p) => p.trim())
    .filter(Boolean);
}

export function NewsDetailOptionB() {
  const a = MOCK_ARTICLE;
  const summaryParas = paragraphs(a.summary);
  const investorParas = paragraphs(a.investorTake);

  return (
    <article className="relative mx-auto max-w-2xl px-4 py-12 sm:py-16">
      <div className="mb-10 flex items-start justify-between gap-3">
        <Badge
          variant="secondary"
          className="bg-muted text-muted-foreground border-transparent text-xs uppercase tracking-widest px-2.5 py-0.5"
        >
          {a.topic}
        </Badge>
        <MockWatchlistButton />
      </div>

      {/* 見出し: text-balance + clamp() で文字数に応じて柔軟にスケール。
          原題は italic + 左 rule で「翻訳タイトル → 原題」の階層を視覚化。 */}
      <header className="mb-12 space-y-4">
        <h1 className="text-balance text-[clamp(1.6rem,1.05rem+1.8vw,2.375rem)] font-medium leading-[1.2] tracking-tight text-foreground">
          {a.translatedTitle}
        </h1>
        <p className="text-pretty border-l-2 border-border/70 pl-3 text-[15px] italic leading-relaxed text-muted-foreground">
          {a.originalTitle}
        </p>
      </header>

      <div className="mb-16 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground">
        <span className="font-medium text-foreground">{a.sourceName}</span>
        <span aria-hidden="true">·</span>
        <span>{a.author}</span>
        <span aria-hidden="true">·</span>
        <span>{formatDate(a.publishedAt, { withTime: true })}</span>
      </div>

      {/* AI Summary: 客観・地のまま。段落分割 + 行間広めで「文字のかたまり」感を解消。 */}
      <section className="space-y-6">
        <header className="space-y-1.5">
          <div className="flex items-center gap-2">
            <FileText
              aria-hidden="true"
              className="size-4 text-muted-foreground"
            />
            <h2 className="text-base font-semibold tracking-tight text-foreground">
              記事の要約
            </h2>
          </div>
          <p className="pl-6 text-xs text-muted-foreground">
            AI が原文を翻訳・要約しています
          </p>
        </header>
        <div className="space-y-5">
          {summaryParas.map((p, i) => (
            <p
              // biome-ignore lint/suspicious/noArrayIndexKey: 静的 mock の段落配列で順序が安定
              key={i}
              className="text-pretty text-[15px] leading-[1.9] text-foreground/95"
            >
              {p}
            </p>
          ))}
        </div>
      </section>

      {/* Investor Take: 主観・引用ブロック扱い */}
      <aside className="relative mt-16 rounded-r-md border-l-2 border-primary bg-secondary/60 px-6 py-7 sm:px-8 sm:py-9">
        <header className="mb-5 space-y-1.5">
          <div className="flex items-center gap-2">
            <Compass aria-hidden="true" className="size-4 text-primary" />
            <h2 className="text-base font-semibold tracking-tight text-foreground">
              Vector の見立て
            </h2>
          </div>
          <p className="pl-6 text-xs text-muted-foreground">
            投資・市場視点での編集解釈
          </p>
        </header>
        <div className="space-y-4">
          {investorParas.map((p, i) => (
            <p
              // biome-ignore lint/suspicious/noArrayIndexKey: 静的 mock の段落配列で順序が安定
              key={i}
              className="text-pretty text-[15px] leading-[1.9] text-foreground/95"
            >
              {p}
            </p>
          ))}
        </div>
      </aside>

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
