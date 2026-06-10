import { ArrowUpRight } from "lucide-react";
import Link from "next/link";
import { PaperByline } from "@/components/paper";
import type { BriefingArticleEmbedParsed } from "../schemas/briefing";

interface ArticleCardProps {
  article: BriefingArticleEmbedParsed;
}

/** 重要記事の見出し (内部詳細リンク) + 外部原文ボタン + 出典・公開日 + key points (紙面様式)。 */
export function ArticleCard({ article }: ArticleCardProps) {
  return (
    <div className="flex flex-col gap-2.5">
      {/* タイトルクリックは内部詳細画面へ */}
      <Link
        href={`/news/${article.id}`}
        className="group inline-block text-[var(--vector-ink)] transition-colors hover:text-[var(--vector-accent-ink)]"
      >
        <span
          className="text-pretty text-[clamp(17px,1.7vw,21px)] font-bold leading-[1.4]"
          style={{ fontFamily: "var(--font-vector-serif)" }}
        >
          {article.translatedTitle}
        </span>
      </Link>

      <PaperByline
        sourceName={article.source.name}
        sourceLabel={article.source.attributionLabel ?? article.source.name}
        publishedAt={article.publishedAt}
      />

      {article.keyPoints.length > 0 && (
        <ul className="flex flex-col gap-1 pl-0">
          {article.keyPoints.map((point, i) => (
            <li
              // biome-ignore lint/suspicious/noArrayIndexKey: 要点順序は AI 出力に従い安定
              key={i}
              className="flex items-start gap-1.5 text-[13px] leading-[1.7] text-[var(--vector-ink-soft)]"
              style={{ fontFamily: "var(--font-vector-maru)" }}
            >
              <span
                aria-hidden="true"
                className="mt-[0.45em] size-1 shrink-0 rounded-full bg-[var(--vector-ink-muted)]"
              />
              {point}
            </li>
          ))}
        </ul>
      )}

      {/* 外部原文へは明示的な別ボタン */}
      <a
        href={article.url}
        target="_blank"
        rel="noopener noreferrer"
        aria-label={`「${article.translatedTitle}」の原文を読む (外部サイト)`}
        className="inline-flex w-fit items-center gap-1 text-[12px] tracking-[0.02em] text-[var(--vector-ink-muted)] transition-colors hover:text-[var(--vector-ink)]"
        style={{ fontFamily: "var(--font-vector-maru)" }}
      >
        原文を読む
        <ArrowUpRight aria-hidden="true" className="size-3" />
      </a>
    </div>
  );
}
