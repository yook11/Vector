import { PaperKicker } from "@/components/paper";
import type { BriefingKeyArticleParsed } from "../schemas/briefing";
import { ArticleCard } from "./ArticleCard";

interface KeyArticleBlockProps {
  keyArticle: BriefingKeyArticleParsed;
  /** 見出し用カテゴリ。briefing は単一カテゴリのため全カード共通。 */
  category: { slug: string; name: string };
  /** 0 始まりの並び順。表示は 01, 02, ... */
  index: number;
}

/** 特に重要な記事 1 件の feature カード: 番号 + カテゴリ + 見出し + 出典 + なぜ重要か。 */
export function KeyArticleBlock({
  keyArticle,
  category,
  index,
}: KeyArticleBlockProps) {
  return (
    <article className="grid grid-cols-[clamp(34px,4vw,46px)_1fr] items-baseline gap-x-[clamp(16px,2.6vw,26px)] border-t border-[var(--vector-line)] pt-6">
      <span
        className="text-[clamp(26px,3vw,34px)] italic leading-none text-[var(--vector-ink-muted)]"
        style={{ fontFamily: "var(--font-vector-display)" }}
      >
        {String(index + 1).padStart(2, "0")}
      </span>
      <div className="flex min-w-0 flex-col gap-3">
        <PaperKicker slug={category.slug} name={category.name} />
        <ArticleCard article={keyArticle.article} />
        <p
          className="text-pretty text-[14.5px] leading-[1.9] text-[var(--vector-ink-soft)]"
          style={{ fontFamily: "var(--font-vector-serif)" }}
        >
          {keyArticle.significance}
        </p>
      </div>
    </article>
  );
}
