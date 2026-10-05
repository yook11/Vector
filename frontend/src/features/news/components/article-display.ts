import type { AnalyzedArticlePreview } from "@/types/types.gen";

/** AnalyzedArticlePreview 依存の紙面表示ヘルパ。design-system 部品 (components/paper) とは別に
 *  news ドメインの型に紐づくためここに残す。 */

export function getArticleSourceLabel(article: AnalyzedArticlePreview): string {
  return article.source.attributionLabel ?? article.source.name;
}

export function getLatestArticleDate(items: AnalyzedArticlePreview[]): Date {
  const timestamps = items
    .map((item) =>
      item.publishedAt ? new Date(item.publishedAt).getTime() : Number.NaN,
    )
    .filter(Number.isFinite);

  if (timestamps.length === 0) return new Date();
  return new Date(Math.max(...timestamps));
}
