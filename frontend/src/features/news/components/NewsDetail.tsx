import { ArrowUpRight, ChevronLeft, Sparkles } from "lucide-react";
import Link from "next/link";
import { WatchlistButton } from "@/features/watchlist";
import { sanitizeUrl } from "@/lib/utils/sanitize-url";
import type { ArticleDetail as ArticleDetailData } from "@/types/types.gen";
import { PaperByline } from "./PaperByline";
import { PaperKicker } from "./PaperKicker";
import { formatPaperDate, formatPaperTime } from "./paper-style";

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
  const contextParagraphs = article.investorTake
    ? toParagraphs(article.investorTake)
    : [];
  const sourceLabel = article.source.attributionLabel ?? article.source.name;

  return (
    <article className="pt-7 pb-4">
      <div className="mb-7">
        <Link
          href="/"
          className="inline-flex items-center gap-1.5 text-[12.5px] tracking-[0.04em] text-[var(--vector-ink-muted)] transition-colors hover:text-[var(--vector-ink)]"
          style={{ fontFamily: "var(--font-vector-maru)" }}
        >
          <ChevronLeft aria-hidden="true" className="size-3.5" />
          ダッシュボードに戻る
        </Link>
      </div>

      {/* 見出し帯: 全幅。翻訳タイトル → 原題 (deck) の階層を罫線と書体で示す。 */}
      <header className="mb-9">
        <div className="mb-4">
          <PaperKicker
            slug={article.category.slug}
            name={article.category.name}
          />
        </div>
        <h1
          className="text-balance text-[clamp(30px,4vw,44px)] font-extrabold leading-[1.32] tracking-[0.01em] text-[var(--vector-ink)]"
          style={{ fontFamily: "var(--font-vector-serif)" }}
        >
          {article.translatedTitle}
        </h1>
        <p
          className="mt-4 max-w-[46em] text-pretty border-l-2 border-[var(--vector-line)] pl-4 text-[16px] italic leading-[1.5] text-[var(--vector-ink-muted)]"
          style={{ fontFamily: "var(--font-vector-display)" }}
        >
          {article.original.title}
        </p>
        <div className="mt-5 flex flex-wrap items-center justify-between gap-4 border-t-[3px] border-double border-[var(--vector-ink)] pt-4">
          <PaperByline
            sourceName={article.source.name}
            sourceLabel={sourceLabel}
            publishedAt={article.publishedAt}
            withTime
          />
          <WatchlistButton
            articleId={article.id}
            isWatched={isWatched}
            className="text-[var(--vector-ink-muted)] hover:bg-transparent hover:text-[var(--vector-accent)]"
            iconClassName="size-[18px]"
          />
        </div>
      </header>

      {/* 本文カラム: measure 860px。読みやすさと右余白の活用を両立する。 */}
      <div className="max-w-[860px]">
        <div className="mb-5">
          <span
            className="inline-flex items-center gap-1.5 text-[13px] italic text-[var(--vector-accent-ink)]"
            style={{ fontFamily: "var(--font-vector-display)" }}
          >
            <Sparkles
              aria-hidden="true"
              className="size-3.5 text-[var(--vector-accent)]"
            />
            AIが原文を翻訳・要約しています
          </span>
        </div>

        {/* 先頭段落はリード (大きめ・字下げなし)、以降は字下げで段落を区切る。 */}
        {summaryParagraphs.map((p, i) => (
          <p
            // biome-ignore lint/suspicious/noArrayIndexKey: 段落順序は AI 出力に従い安定
            key={i}
            className={
              i === 0
                ? "mb-[1.35em] text-pretty text-[17px] leading-[1.95] text-[var(--vector-ink)]"
                : "mb-[1.35em] text-pretty text-[15.5px] leading-[2.0] text-[var(--vector-ink-soft)] [text-indent:1em]"
            }
            style={{ fontFamily: "var(--font-vector-serif)" }}
          >
            {p}
          </p>
        ))}

        {/* 背景ノート: 投資助言ではなく中立的な編集部の背景整理として上下罫で示す。 */}
        {contextParagraphs.length > 0 && (
          <section className="my-9 py-6 [border-bottom:1px_solid_var(--vector-line)] [border-top:1px_solid_var(--vector-ink)]">
            <div className="mb-3.5 flex flex-wrap items-baseline gap-3">
              <span
                className="text-[13px] font-semibold uppercase tracking-[0.26em] text-[var(--vector-accent-ink)]"
                style={{ fontFamily: "var(--font-vector-display)" }}
              >
                CONTEXT
              </span>
              <span
                className="text-[17px] font-bold text-[var(--vector-ink)]"
                style={{ fontFamily: "var(--font-vector-serif)" }}
              >
                文脈
              </span>
              <span
                className="text-[11px] tracking-[0.04em] text-[var(--vector-ink-muted)]"
                style={{ fontFamily: "var(--font-vector-maru)" }}
              >
                編集部による背景整理
              </span>
            </div>
            <div className="space-y-4">
              {contextParagraphs.map((p, i) => (
                <p
                  // biome-ignore lint/suspicious/noArrayIndexKey: 段落順序は AI 出力に従い安定
                  key={i}
                  className="text-pretty text-[14.5px] leading-[2.0] text-[var(--vector-ink-soft)]"
                  style={{ fontFamily: "var(--font-vector-serif)" }}
                >
                  {p}
                </p>
              ))}
            </div>
          </section>
        )}

        <div className="mt-9 flex flex-wrap items-center gap-4">
          {safeUrl !== null && (
            <Link
              href={safeUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-2 rounded-full border border-[var(--vector-ink)] px-[18px] py-2.5 text-[13px] text-[var(--vector-ink)] transition-colors hover:bg-[color-mix(in_oklab,var(--vector-ink)_6%,transparent)]"
              style={{ fontFamily: "var(--font-vector-maru)" }}
            >
              原文を読む
              <ArrowUpRight aria-hidden="true" className="size-3.5" />
            </Link>
          )}
          <span
            className="text-[12.5px] italic text-[var(--vector-ink-muted)]"
            style={{ fontFamily: "var(--font-vector-display)" }}
          >
            Analyzed at {formatPaperDate(article.analyzedAt)}{" "}
            {formatPaperTime(article.analyzedAt)}
          </span>
        </div>
      </div>
    </article>
  );
}
