import type { Category } from "@/types/types.gen";

interface ArticleListHeadingProps {
  selectedCategorySlug?: string;
  categories: Category[];
}

/** フィルタバー左の見出し。表示中のカテゴリ名を出す。 */
export function ArticleListHeading({
  categories,
  selectedCategorySlug,
}: ArticleListHeadingProps) {
  // 未知 slug (rename 後の stale URL 等) は内部 slug を露出させず、存在しないカテゴリとして示す。
  const categoryName =
    selectedCategorySlug === undefined
      ? "すべて"
      : (categories.find((category) => category.slug === selectedCategorySlug)
          ?.name ?? "存在しないカテゴリ");

  return (
    <span
      className="text-[14px] font-semibold whitespace-nowrap text-[var(--vector-ink)]"
      style={{ fontFamily: "var(--font-vector-display)" }}
    >
      {categoryName}
    </span>
  );
}
