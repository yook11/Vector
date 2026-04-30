/**
 * news 一覧用 URL search params の SSR 側パーサ (純関数)。
 *
 * `app/(protected)/page.tsx` / `app/(protected)/watchlist/page.tsx` の Server
 * Component から `searchParams` を `ArticleQuery` に正規化する。zod preprocess
 * helper (`SingleString` / `PositiveIntFromString` / `SortOrder`) は news 同梱
 * (現状 news 以外の caller なし。将来再利用要件が出た時点で `lib/zod-helpers/`
 * に昇格する想定)。
 *
 * Client 側のフック (`useUpdateSearchParams` 等) は `lib/search-params/client.ts`
 * に残留 (URL 更新自体は cross-cutting)。
 */

import { z } from "zod";
import type { SearchParams } from "@/lib/types/route";
import type { ArticleQuery } from "@/types";

// raw searchParams は string | string[] | undefined。string 単一値以外は
// undefined に丸めて zod に渡し、未指定キーと同等に扱う。
const SingleString = z.preprocess(
  (v) => (typeof v === "string" ? v : undefined),
  z.string().optional(),
);

const PositiveIntFromString = z.preprocess((v) => {
  if (typeof v !== "string" || v === "") return undefined;
  const n = Number(v);
  return Number.isFinite(n) ? n : undefined;
}, z.number().int().positive().optional());

const SortOrder = z.preprocess(
  (v) => (v === "asc" || v === "desc" ? v : undefined),
  z.enum(["asc", "desc"]).optional(),
);

const ArticleQueryParamsSchema = z.object({
  category: SingleString,
  sortOrder: SortOrder,
  page: PositiveIntFromString,
  perPage: PositiveIntFromString,
  q: SingleString,
});

/**
 * SSR の `searchParams` を `ArticleQuery` + 検索クエリ q に正規化する。
 * 数値は NaN を弾き、未指定キーはオブジェクトに含めない。
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
