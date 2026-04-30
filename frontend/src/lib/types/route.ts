/**
 * Next.js App Router の searchParams 生型。
 *
 * Next.js 16 では Page Component の `searchParams` prop は
 * `Promise<Record<string, string | string[] | undefined>>` (multi-value
 * クエリ + 未指定キーの 3 状態)。`features/news/search-params.ts` の
 * `parseArticleQuery` で zod に通して `ArticleQuery` に正規化する。
 */
export type SearchParams = Record<string, string | string[] | undefined>;
