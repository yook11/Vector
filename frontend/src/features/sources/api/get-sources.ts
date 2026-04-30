import { serverFetch } from "@/lib/api/server-fetcher";
import type { NewsSourceDetailList } from "@/types";

/**
 * Fetch all news sources (admin only, SSR).
 *
 * Next.js 16 公式は `'use cache'` directive を新規実装の default として推奨
 * する (https://nextjs.org/docs/app/api-reference/directives/use-cache)。
 * ただし `'use cache'` 内では cookies()/headers() の読み取りが構造的に
 * 禁止されている (hard constraint)。本関数は admin 認証に依存する
 * `serverFetch` 経由なので `'use cache'` 内では実行不可。
 *
 * 代替として legacy fetch options (`next: { revalidate, tags }`) を採用する。
 * tag "sources" は同 feature 内 4 Server Action (create / activate /
 * deactivate / delete) の `updateTag("sources")` で immediate 無効化される
 * ため、`revalidate: 7200` は updateTag が動かなかった場合の fallback
 * expiration として機能する。
 */
export async function getSources(): Promise<NewsSourceDetailList> {
  return serverFetch<NewsSourceDetailList>("/admin/sources", {
    next: { revalidate: 7200, tags: ["sources"] },
  });
}
