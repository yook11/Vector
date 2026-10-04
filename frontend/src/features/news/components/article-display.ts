import type { ArticleBrief } from "@/types/types.gen";

/** ArticleBrief 依存の紙面表示ヘルパ。design-system 部品 (components/paper) とは別に
 *  news ドメインの型に紐づくためここに残す。 */

export function getArticleSourceLabel(article: ArticleBrief): string {
  return article.source.attributionLabel ?? article.source.name;
}

export function getLatestArticleDate(items: ArticleBrief[]): Date {
  const timestamps = items
    .map((item) =>
      item.publishedAt ? new Date(item.publishedAt).getTime() : Number.NaN,
    )
    .filter(Number.isFinite);

  if (timestamps.length === 0) return new Date();
  return new Date(Math.max(...timestamps));
}
