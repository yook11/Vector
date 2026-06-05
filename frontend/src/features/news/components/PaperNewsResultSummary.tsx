import type { ArticleQuery } from "@/types";
import type { CategoryDetail } from "@/types/types.gen";
import { getArticles } from "../api/get-articles";

interface PaperNewsResultSummaryProps {
  activeCategory?: string;
  categories: CategoryDetail[];
  filters: ArticleQuery;
}

/** フィルタバー左の結果サマリ「<カテゴリ> · 全 N件」。total は cached getArticles を共用。 */
export async function PaperNewsResultSummary({
  activeCategory,
  categories,
  filters,
}: PaperNewsResultSummaryProps) {
  const { total } = await getArticles(filters);
  // 未知 slug (rename 後の stale URL 等) は内部 slug を露出させず「すべて」に倒す。
  const categoryName =
    activeCategory === undefined
      ? "すべて"
      : (categories.find((category) => category.slug === activeCategory)
          ?.name ?? "すべて");

  return (
    <span
      className="inline-flex items-center gap-2 whitespace-nowrap text-[12.5px] text-[var(--vector-ink-soft)]"
      style={{ fontFamily: "var(--font-vector-maru)" }}
    >
      <span
        className="text-[14px] font-semibold text-[var(--vector-ink)]"
        style={{ fontFamily: "var(--font-vector-display)" }}
      >
        {categoryName}
      </span>
      <span className="text-[var(--vector-ink-muted)]">·</span>
      <span>
        全 <b className="font-bold text-[var(--vector-accent-ink)]">{total}</b>{" "}
        件
      </span>
    </span>
  );
}
