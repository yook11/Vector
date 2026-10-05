/**
 * news 一覧用 URL search params の SSR 側パーサ (純関数)。
 *
 * `app/(public)/page.tsx` の Server Component から `searchParams` を
 * `ArticleQuery` に正規化する。URL に載せる条件は category だけで、続きの位置
 * (cursor) は URL に載せない。
 */

import { z } from "zod";
import type { SearchParams } from "@/lib/types/route";
import type { ArticleQuery } from "@/types";

export const CATEGORY_SLUG_PATTERN = /^[a-z0-9][a-z0-9_]{0,49}$/;

const CategorySlug = z.preprocess((v) => {
  if (typeof v !== "string") return undefined;
  const slug = v.trim();
  return CATEGORY_SLUG_PATTERN.test(slug) ? slug : undefined;
}, z.string().optional());

const ArticleQueryParamsSchema = z.object({
  category: CategorySlug,
});

/**
 * SSR の `searchParams` を `ArticleQuery` に正規化する。
 * 無効値・配列値は未指定扱いにし、オブジェクトに含めない。廃止した
 * page / perPage / sortOrder など未知のパラメータは無視する。
 */
export function parseArticleQuery(raw: SearchParams): {
  query: ArticleQuery;
} {
  const result = ArticleQueryParamsSchema.safeParse(raw);
  // schema の各フィールドは preprocess 段階で型不一致を undefined に丸めるため
  // safeParse は基本 success になる。failure は schema 側のバグ扱いで空クエリへ。
  const data = result.success ? result.data : {};

  const query: ArticleQuery = {};
  if (data.category) query.category = data.category;

  return { query };
}
