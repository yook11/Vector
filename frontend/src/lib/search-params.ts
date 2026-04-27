/**
 * URL search params の SSR 側ユーティリティ (純関数のみ)。
 *
 * `app/(protected)/page.tsx` / `app/(protected)/watchlist/page.tsx` の
 * Server Component から `searchParams` を `ArticleQuery` に正規化するために使う。
 *
 * Client 側のフック (`useUpdateSearchParams` 等) は `search-params-client.ts`。
 */

import type { ArticleQuery } from "@/types";

type RawSearchParams = Record<string, string | string[] | undefined>;

/**
 * SSR の `searchParams` を `ArticleQuery` + 検索クエリ q に正規化する。
 * 数値は NaN を弾き、未指定キーはオブジェクトに含めない。
 */
export function parseArticleQuery(raw: RawSearchParams): {
  query: ArticleQuery;
  q?: string;
} {
  const str = (key: string): string | undefined => {
    const v = raw[key];
    return typeof v === "string" ? v : undefined;
  };
  const num = (key: string): number | undefined => {
    const s = str(key);
    if (!s) return undefined;
    const n = Number(s);
    return Number.isFinite(n) ? n : undefined;
  };

  const query: ArticleQuery = {};

  const category = str("category");
  if (category) query.category = category;

  const source = str("source");
  if (source) query.source = source;

  const sortOrder = str("sortOrder");
  if (sortOrder === "asc" || sortOrder === "desc") {
    query.sortOrder = sortOrder;
  }

  const page = num("page");
  if (page !== undefined) query.page = page;

  const perPage = num("perPage");
  if (perPage !== undefined) query.perPage = perPage;

  return { query, q: str("q") };
}
