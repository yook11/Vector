import { Compass, ExternalLink } from "lucide-react";
import Link from "next/link";
import { formatDate } from "@/lib/date";
import { MOCK_ARTICLE } from "../_lib/mock-data";
import { MockWatchlistButton } from "./mock-watchlist-button";

export function NewsDetailOptionC() {
  const a = MOCK_ARTICLE;
  return (
    <article className="relative mx-auto max-w-2xl px-4 py-12 sm:py-16">
      {/* === 領域 1: 記事 (翻訳・要約) === */}
      <div className="mb-8 flex items-start justify-end">
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

      <div className="mb-12 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground">
        <span className="font-medium text-foreground">{a.sourceName}</span>
        <span aria-hidden="true">·</span>
        <span>{a.author}</span>
        <span aria-hidden="true">·</span>
        <span>{formatDate(a.publishedAt, { withTime: true })}</span>
      </div>

      {/* 記事本文 (= AI 要約) はラベルを薄め、読み物として流す */}
      <p className="text-base leading-relaxed text-foreground">{a.summary}</p>

      <div className="mt-10 flex items-center gap-3 text-xs text-muted-foreground">
        <Link
          href={a.url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1.5 font-medium text-foreground transition-colors hover:text-primary"
        >
          原文を読む
          <ExternalLink aria-hidden="true" className="size-3" />
        </Link>
        <span aria-hidden="true">·</span>
        <span>原文を AI が翻訳・要約しています</span>
      </div>

      {/* === 領域変更マーカー: ここから Vector の編集領域 === */}
      <div
        className="my-20 flex items-center gap-4 sm:my-24"
        aria-hidden="true"
      >
        <span className="h-px flex-1 bg-border" />
        <div className="flex items-center gap-2 text-muted-foreground">
          <Compass className="size-3.5" />
          <span className="text-[11px] font-medium uppercase tracking-[0.22em]">
            Vector Analysis
          </span>
        </div>
        <span className="h-px flex-1 bg-border" />
      </div>

      {/* === 領域 2: Vector による分析 === */}
      <section>
        <header className="mb-8 space-y-2">
          <h2 className="text-2xl font-medium tracking-tight text-foreground sm:text-[26px]">
            この記事への投資視点
          </h2>
          <p className="text-sm text-muted-foreground">
            Vector
            編集チームによる市場・投資判断の参考。記事内容の客観要約とは独立した解釈です。
          </p>
        </header>
        <p className="text-base leading-relaxed text-foreground">
          {a.investorTake}
        </p>
      </section>

      <footer className="mt-16 border-t border-border/60 pt-8">
        <p className="text-xs text-muted-foreground">
          Analyzed at {formatDate(a.analyzedAt, { withTime: true })}
        </p>
      </footer>
    </article>
  );
}
