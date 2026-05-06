/**
 * news 一覧用 URL search params の SSR 側パーサ (純関数)。
 *
 * `app/(protected)/page.tsx` / `app/(protected)/watchlist/page.tsx` の Server
 * Component から `searchParams` を `ArticleQuery` に正規化する。zod preprocess
 * helper (`CategorySlug` / `boundedIntFromString` / `SortOrder`) は news 同梱
 * (現状 news 以外の caller なし。将来再利用要件が出た時点で `lib/zod-helpers/`
 * に昇格する想定)。
 *
 * Client 側のフック (`useUpdateSearchParams` 等) は `lib/search-params/client.ts`
 * に残留 (URL 更新自体は cross-cutting)。
 */

import { z } from "zod";
import type { SearchParams } from "@/lib/types/route";
import type { ArticleQuery } from "@/types";

const CATEGORY_SLUG_PATTERN = /^[a-z0-9][a-z0-9_]{0,49}$/;
const MAX_PAGE = 10_000;
const MAX_PER_PAGE = 100;
const MAX_SEARCH_QUERY_LENGTH = 200;

const CategorySlug = z.preprocess((v) => {
  if (typeof v !== "string") return undefined;
  const slug = v.trim();
  return CATEGORY_SLUG_PATTERN.test(slug) ? slug : undefined;
}, z.string().optional());

const SearchQuery = z.preprocess((v) => {
  if (typeof v !== "string") return undefined;
  const q = v.trim();
  if (!q || q.length > MAX_SEARCH_QUERY_LENGTH) return undefined;
  return q;
}, z.string().optional());

function boundedIntFromString(max: number) {
  return z.preprocess((v) => {
    if (typeof v !== "string") return undefined;
    const s = v.trim();
    if (!/^\d+$/.test(s)) return undefined;
    const n = Number(s);
    if (!Number.isSafeInteger(n) || n < 1 || n > max) return undefined;
    return n;
  }, z.number().int().positive().optional());
}

const SortOrder = z.preprocess(
  (v) => (v === "asc" || v === "desc" ? v : undefined),
  z.enum(["asc", "desc"]).optional(),
);

const ArticleQueryParamsSchema = z.object({
  category: CategorySlug,
  sortOrder: SortOrder,
  page: boundedIntFromString(MAX_PAGE),
  perPage: boundedIntFromString(MAX_PER_PAGE),
  q: SearchQuery,
});

/**
 * SSR の `searchParams` を `ArticleQuery` + 検索クエリ q に正規化する。
 * 無効値・配列値・範囲外の値は未指定扱いにし、オブジェクトに含めない。
 */
export function parseArticleQuery(raw: SearchParams): {
  query: ArticleQuery;
  q?: string;
} {
  const result = ArticleQueryParamsSchema.safeParse(raw);
  // schema の各フィールドは preprocess 段階で型不一致を undefined に丸めるため
  // safeParse は基本 success になる。failure は schema 側のバグ扱いで空クエリへ。
  const data = result.success ? result.data : {};
  const { q, ...rest } = data;

  const query: ArticleQuery = {};
  if (rest.category) query.category = rest.category;
  if (rest.sortOrder) query.sortOrder = rest.sortOrder;
  if (rest.page !== undefined) query.page = rest.page;
  if (rest.perPage !== undefined) query.perPage = rest.perPage;

  // exactOptionalPropertyTypes 下で q?: string に undefined を明示代入
  // できないため、未指定時は q キー自体を省く。
  return q !== undefined ? { query, q } : { query };
}
