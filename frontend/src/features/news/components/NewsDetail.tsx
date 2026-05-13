import { Compass, ExternalLink, FileText } from "lucide-react";
import Link from "next/link";
import { WatchlistButton } from "@/features/watchlist";
import { formatDate } from "@/lib/date";
import { sanitizeUrl } from "@/lib/utils/sanitize-url";
import type { ArticleDetail as ArticleDetailData } from "@/types/types.gen";

interface NewsDetailProps {
  article: ArticleDetailData;
  /** Pattern B: ウォッチ状態は record の外から注入する。 */
  isWatched: boolean;
}

function toParagraphs(text: string): string[] {
  return text
    .split(/\n\n+/)
    .map((p) => p.trim())
    .filter(Boolean);
}

export function NewsDetail({ article, isWatched }: NewsDetailProps) {
  // --- XSS: validate URL scheme (reject javascript: etc.) ---
  const safeUrl = sanitizeUrl(article.original.url);
  const summaryParagraphs = toParagraphs(article.summary);
  const investorParagraphs = article.investorTake
    ? toParagraphs(article.investorTake)
    : [];

  return (
    <article className="relative mx-auto max-w-2xl px-4 py-12 sm:py-16">
      <div className="mb-10 flex items-start justify-end">
        <WatchlistButton articleId={article.id} isWatched={isWatched} />
      </div>

      {/* 見出し: text-balance + clamp() で文字数に応じて柔軟にスケール。
          原題は italic + 左罫線で「翻訳タイトル → 原題」の階層を視覚化。 */}
      <header className="mb-12 space-y-4">
        <h1 className="text-balance text-[clamp(1.6rem,1.05rem+1.8vw,2.375rem)] font-medium leading-[1.2] tracking-tight text-foreground">
          {article.translatedTitle}
        </h1>
        <p className="text-pretty border-l-2 border-border/70 pl-3 text-[15px] italic leading-relaxed text-muted-foreground">
          {article.original.title}
        </p>
      </header>

      <div className="mb-16 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground">
        <span className="font-medium text-foreground">
          {article.source.name}
        </span>
        <span aria-hidden="true">·</span>
        <span>{formatDate(article.publishedAt, { withTime: true })}</span>
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
          {summaryParagraphs.map((p, i) => (
            <p
              // biome-ignore lint/suspicious/noArrayIndexKey: 段落順序は AI 出力に従い安定
              key={i}
              className="text-pretty text-[15px] leading-[1.9] text-foreground/95"
            >
              {p}
            </p>
          ))}
        </div>
      </section>

      {/* Investor Take: 主観・引用ブロック扱い (left border + 薄背景 + Compass icon) */}
      {investorParagraphs.length > 0 && (
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
            {investorParagraphs.map((p, i) => (
              <p
                // biome-ignore lint/suspicious/noArrayIndexKey: 段落順序は AI 出力に従い安定
                key={i}
                className="text-pretty text-[15px] leading-[1.9] text-foreground/95"
              >
                {p}
              </p>
            ))}
          </div>
        </aside>
      )}

      <footer className="mt-16 flex flex-col items-start gap-4 border-t border-border/60 pt-10">
        {safeUrl !== null && (
          <Link
            href={safeUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 rounded-full border border-border px-5 py-2.5 text-sm font-medium text-foreground transition-colors hover:bg-accent"
          >
            原文を読む
            <ExternalLink aria-hidden="true" className="size-3.5" />
          </Link>
        )}
        <p className="text-xs text-muted-foreground">
          Analyzed at {formatDate(article.analyzedAt, { withTime: true })}
        </p>
      </footer>
    </article>
  );
}
